"""以有界内存对二进制 JSON 输入执行严格语法验证。"""

from __future__ import annotations

import codecs
import enum
import re
import tempfile
import threading
from typing import BinaryIO, Final, NoReturn

_READ_SIZE: Final = 64 * 1024
_CANCELLATION_CHECK_INTERVAL: Final = 4096
_STRING_SPECIAL_CHARACTERS: Final = re.compile(r'["\\\x00-\x1f]')
_JSON_WHITESPACE: Final = frozenset(" \t\r\n")
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES: Final = frozenset('"\\/bfnrt')


class JsonStreamValidationError(ValueError):
    """JSON 文本、编码或语法不合法。"""

    def __init__(self, code: str = "json_stream_invalid_json") -> None:
        """保存可供上层映射的稳定错误码。"""
        super().__init__(code)
        self.code = code


class JsonStreamValidationCancelled(RuntimeError):
    """调用方取消触发的流式 JSON 验证中止。"""

    def __init__(self, code: str = "json_stream_validation_cancelled") -> None:
        """保存可供上层识别的取消错误码。"""
        super().__init__(code)
        self.code = code


class _Container(enum.IntEnum):
    """文件后备容器栈中的容器类型。"""

    ARRAY = 1
    OBJECT = 2


class _Expectation(enum.IntEnum):
    """语法状态机当前期待的下一个 JSON 结构。"""

    VALUE = 1
    ARRAY_VALUE_OR_END = 2
    ARRAY_COMMA_OR_END = 3
    OBJECT_KEY_OR_END = 4
    OBJECT_KEY = 5
    OBJECT_COLON = 6
    OBJECT_COMMA_OR_END = 7
    ROOT_END = 8


class _Mode(enum.IntEnum):
    """当前正在验证的标量或结构模式。"""

    NORMAL = 1
    STRING = 2
    STRING_ESCAPE = 3
    STRING_UNICODE = 4
    NUMBER = 5
    LITERAL = 6


class _NumberState(enum.IntEnum):
    """JSON 数字的有限状态。"""

    MINUS = 1
    ZERO = 2
    INTEGER = 3
    DECIMAL_POINT = 4
    FRACTION = 5
    EXPONENT = 6
    EXPONENT_SIGN = 7
    EXPONENT_DIGITS = 8


class _JsonSyntaxParser:
    """以非递归状态机验证 JSON，并把容器栈写入临时文件。"""

    def __init__(
        self,
        stack_file: BinaryIO,
        cancellation: threading.Event | None,
    ) -> None:
        """初始化解析状态。

        参数:
            stack_file: 可随机读写的二进制临时文件，用于保存容器类型栈。
            cancellation: 调用方取消事件；为空时不响应取消。

        返回:
            无返回值。

        异常:
            不主动抛出业务异常。
        """
        self._stack_file = stack_file
        self._stack_depth = 0
        self._cancellation = cancellation
        self._expectation = _Expectation.VALUE
        self._mode = _Mode.NORMAL
        self._number_state: _NumberState | None = None
        self._string_is_key = False
        self._unicode_remaining = 0
        self._literal = ""
        self._literal_index = 0
        self._processed_since_check = 0

    def feed(self, text: str) -> None:
        """处理一个已增量解码的完整 UTF-8 文本块。

        参数:
            text: 增量 UTF-8 解码器输出的文本块，长度受读取块大小约束。

        返回:
            无返回值。

        异常:
            遇到非法 JSON 时抛出 JsonStreamValidationError；取消事件已设置时
            抛出 JsonStreamValidationCancelled。
        """
        position = 0
        text_length = len(text)
        while position < text_length:
            if self._mode is _Mode.STRING:
                match = _STRING_SPECIAL_CHARACTERS.search(text, position)
                if match is None:
                    self._processed_since_check += text_length - position
                    self.check_cancellation()
                    return
                skipped = match.start() - position
                if skipped:
                    self._processed_since_check += skipped
                    self.check_cancellation()
                    position = match.start()

            self._process_character(text[position])
            position += 1
            self._processed_since_check += 1
            if self._processed_since_check >= _CANCELLATION_CHECK_INTERVAL:
                self._processed_since_check = 0
                self.check_cancellation()
        self.check_cancellation()

    def finish(self) -> None:
        """在 EOF 后完成数字状态并验证根值及容器均已闭合。

        参数:
            无。

        返回:
            无返回值。

        异常:
            JSON 被截断、容器未闭合或根值缺失时抛出
            JsonStreamValidationError；取消事件已设置时抛出
            JsonStreamValidationCancelled。
        """
        self.check_cancellation()
        if self._mode is _Mode.NUMBER:
            if not self._number_is_accepting():
                self._invalid()
            self._mode = _Mode.NORMAL
            self._number_state = None
            self._complete_value()
        elif self._mode is not _Mode.NORMAL:
            self._invalid()
        if self._stack_depth != 0 or self._expectation is not _Expectation.ROOT_END:
            self._invalid()

    def check_cancellation(self) -> None:
        """检查调用方是否已请求取消。

        参数:
            无。

        返回:
            无返回值。

        异常:
            取消事件已设置时抛出 JsonStreamValidationCancelled。
        """
        if self._cancellation is not None and self._cancellation.is_set():
            raise JsonStreamValidationCancelled()

    def _process_character(self, character: str) -> None:
        """按当前模式处理一个字符。

        参数:
            character: 当前待处理字符。

        返回:
            无返回值。

        异常:
            JSON 语法非法时抛出 JsonStreamValidationError。
        """
        while True:
            if self._mode is _Mode.STRING:
                self._process_string_character(character)
                return
            if self._mode is _Mode.STRING_ESCAPE:
                self._process_string_escape(character)
                return
            if self._mode is _Mode.STRING_UNICODE:
                self._process_string_unicode(character)
                return
            if self._mode is _Mode.LITERAL:
                self._process_literal_character(character)
                return
            if self._mode is _Mode.NUMBER:
                if self._consume_number_character(character):
                    return
                if not self._number_is_accepting():
                    self._invalid()
                self._mode = _Mode.NORMAL
                self._number_state = None
                self._complete_value()
                continue
            self._process_normal_character(character)
            return

    def _process_normal_character(self, character: str) -> None:
        """处理字符串和数字之外的语法字符。

        参数:
            character: 当前待处理字符。

        返回:
            无返回值。

        异常:
            当前结构位置不接受该字符时抛出 JsonStreamValidationError。
        """
        if character in _JSON_WHITESPACE:
            return
        if self._expectation is _Expectation.ROOT_END:
            self._invalid()
        if self._expectation is _Expectation.VALUE:
            self._start_value(character)
            return
        if self._expectation is _Expectation.ARRAY_VALUE_OR_END:
            if character == "]":
                self._close_array()
            else:
                self._start_value(character)
            return
        if self._expectation is _Expectation.ARRAY_COMMA_OR_END:
            if character == "]":
                self._close_array()
            elif character == "," and self._top_is(_Container.ARRAY):
                self._expectation = _Expectation.VALUE
            else:
                self._invalid()
            return
        if self._expectation is _Expectation.OBJECT_KEY_OR_END:
            if character == "}":
                self._close_object()
            elif character == '"':
                self._start_string(is_key=True)
            else:
                self._invalid()
            return
        if self._expectation is _Expectation.OBJECT_KEY:
            if character != '"':
                self._invalid()
            self._start_string(is_key=True)
            return
        if self._expectation is _Expectation.OBJECT_COLON:
            if character != ":":
                self._invalid()
            self._expectation = _Expectation.VALUE
            return
        if self._expectation is _Expectation.OBJECT_COMMA_OR_END:
            if character == "}":
                self._close_object()
            elif character == "," and self._top_is(_Container.OBJECT):
                self._expectation = _Expectation.OBJECT_KEY
            else:
                self._invalid()
            return
        self._invalid()

    def _start_value(self, character: str) -> None:
        """从当前字符开始一个 JSON 值。

        参数:
            character: 值起始字符。

        返回:
            无返回值。

        异常:
            字符不能开始 JSON 值时抛出 JsonStreamValidationError。
        """
        if character == '"':
            self._start_string(is_key=False)
            return
        if character == "[":
            self._push_container(_Container.ARRAY)
            self._expectation = _Expectation.ARRAY_VALUE_OR_END
            return
        if character == "{":
            self._push_container(_Container.OBJECT)
            self._expectation = _Expectation.OBJECT_KEY_OR_END
            return
        if character in {"t", "f", "n"}:
            self._start_literal(
                "true" if character == "t" else "false" if character == "f" else "null"
            )
            return
        if character == "-":
            self._start_number(_NumberState.MINUS)
            return
        if character == "0":
            self._start_number(_NumberState.ZERO)
            return
        if "1" <= character <= "9":
            self._start_number(_NumberState.INTEGER)
            return
        self._invalid()

    def _start_string(self, *, is_key: bool) -> None:
        """进入字符串或对象键验证状态。

        参数:
            is_key: True 表示该字符串是对象键。

        返回:
            无返回值。

        异常:
            不主动抛出业务异常。
        """
        self._mode = _Mode.STRING
        self._string_is_key = is_key

    def _process_string_character(self, character: str) -> None:
        """处理字符串普通状态中的特殊字符。

        参数:
            character: 引号、反斜杠或未转义控制字符。

        返回:
            无返回值。

        异常:
            出现未转义控制字符时抛出 JsonStreamValidationError。
        """
        if character == '"':
            self._mode = _Mode.NORMAL
            if self._string_is_key:
                self._expectation = _Expectation.OBJECT_COLON
            else:
                self._complete_value()
            return
        if character == "\\":
            self._mode = _Mode.STRING_ESCAPE
            return
        self._invalid()

    def _process_string_escape(self, character: str) -> None:
        """处理字符串反斜杠后的转义字符。

        参数:
            character: 反斜杠后的字符。

        返回:
            无返回值。

        异常:
            转义字符非法时抛出 JsonStreamValidationError。
        """
        if character == "u":
            self._mode = _Mode.STRING_UNICODE
            self._unicode_remaining = 4
            return
        if character not in _SIMPLE_ESCAPES:
            self._invalid()
        self._mode = _Mode.STRING

    def _process_string_unicode(self, character: str) -> None:
        """处理 Unicode 转义中的一个十六进制字符。

        参数:
            character: 当前十六进制字符。

        返回:
            无返回值。

        异常:
            字符不是十六进制数字时抛出 JsonStreamValidationError。
        """
        if character not in _HEX_DIGITS:
            self._invalid()
        self._unicode_remaining -= 1
        if self._unicode_remaining == 0:
            self._mode = _Mode.STRING

    def _start_literal(self, target: str) -> None:
        """开始验证 true、false 或 null。

        参数:
            target: 目标字面量。

        返回:
            无返回值。

        异常:
            不主动抛出业务异常。
        """
        self._mode = _Mode.LITERAL
        self._literal = target
        self._literal_index = 1

    def _process_literal_character(self, character: str) -> None:
        """逐字符验证当前 JSON 字面量。

        参数:
            character: 当前字面量字符。

        返回:
            无返回值。

        异常:
            字面量字符不匹配时抛出 JsonStreamValidationError。
        """
        if (
            self._literal_index >= len(self._literal)
            or character != self._literal[self._literal_index]
        ):
            self._invalid()
        self._literal_index += 1
        if self._literal_index == len(self._literal):
            self._mode = _Mode.NORMAL
            self._literal = ""
            self._literal_index = 0
            self._complete_value()

    def _start_number(self, state: _NumberState) -> None:
        """进入 JSON 数字验证状态。

        参数:
            state: 已消费首个数字字符后的状态。

        返回:
            无返回值。

        异常:
            不主动抛出业务异常。
        """
        self._mode = _Mode.NUMBER
        self._number_state = state

    def _consume_number_character(self, character: str) -> bool:
        """尝试把字符消费到当前数字状态。

        参数:
            character: 当前待处理字符。

        返回:
            True 表示字符属于当前数字；False 表示数字遇到分隔符或非法字符。

        异常:
            不主动抛出业务异常。
        """
        state = self._number_state
        if state is _NumberState.MINUS:
            if character == "0":
                self._number_state = _NumberState.ZERO
                return True
            if "1" <= character <= "9":
                self._number_state = _NumberState.INTEGER
                return True
            return False
        if state is _NumberState.ZERO:
            if character in ".eE":
                self._number_state = (
                    _NumberState.DECIMAL_POINT
                    if character == "."
                    else _NumberState.EXPONENT
                )
                return True
            return False
        if state is _NumberState.INTEGER:
            if "0" <= character <= "9":
                return True
            if character in ".eE":
                self._number_state = (
                    _NumberState.DECIMAL_POINT
                    if character == "."
                    else _NumberState.EXPONENT
                )
                return True
            return False
        if state is _NumberState.DECIMAL_POINT:
            if "0" <= character <= "9":
                self._number_state = _NumberState.FRACTION
                return True
            return False
        if state is _NumberState.FRACTION:
            if "0" <= character <= "9":
                return True
            if character in "eE":
                self._number_state = _NumberState.EXPONENT
                return True
            return False
        if state is _NumberState.EXPONENT:
            if character in "+-":
                self._number_state = _NumberState.EXPONENT_SIGN
                return True
            if "0" <= character <= "9":
                self._number_state = _NumberState.EXPONENT_DIGITS
                return True
            return False
        if state is _NumberState.EXPONENT_SIGN:
            if "0" <= character <= "9":
                self._number_state = _NumberState.EXPONENT_DIGITS
                return True
            return False
        if state is _NumberState.EXPONENT_DIGITS:
            return "0" <= character <= "9"
        self._invalid()

    def _number_is_accepting(self) -> bool:
        """判断当前数字状态能否在分隔符或 EOF 处结束。

        参数:
            无。

        返回:
            True 表示当前数字语法完整。

        异常:
            不主动抛出业务异常。
        """
        return self._number_state in {
            _NumberState.ZERO,
            _NumberState.INTEGER,
            _NumberState.FRACTION,
            _NumberState.EXPONENT_DIGITS,
        }

    def _complete_value(self) -> None:
        """根据容器栈把一次完整值推进到父级语法状态。

        参数:
            无。

        返回:
            无返回值。

        异常:
            容器栈不合法时抛出 JsonStreamValidationError。
        """
        if self._stack_depth == 0:
            self._expectation = _Expectation.ROOT_END
        elif self._stack_peek() is _Container.ARRAY:
            self._expectation = _Expectation.ARRAY_COMMA_OR_END
        else:
            self._expectation = _Expectation.OBJECT_COMMA_OR_END

    def _close_array(self) -> None:
        """验证并关闭当前数组容器。

        参数:
            无。

        返回:
            无返回值。

        异常:
            当前栈顶不是数组或语法状态不接受右方括号时抛出
            JsonStreamValidationError。
        """
        if (
            self._expectation
            not in {
                _Expectation.ARRAY_VALUE_OR_END,
                _Expectation.ARRAY_COMMA_OR_END,
            }
            or not self._top_is(_Container.ARRAY)
        ):
            self._invalid()
        self._stack_depth -= 1
        self._complete_value()

    def _close_object(self) -> None:
        """验证并关闭当前对象容器。

        参数:
            无。

        返回:
            无返回值。

        异常:
            当前栈顶不是对象或语法状态不接受右花括号时抛出
            JsonStreamValidationError。
        """
        if (
            self._expectation
            not in {
                _Expectation.OBJECT_KEY_OR_END,
                _Expectation.OBJECT_COMMA_OR_END,
            }
            or not self._top_is(_Container.OBJECT)
        ):
            self._invalid()
        self._stack_depth -= 1
        self._complete_value()

    def _push_container(self, container: _Container) -> None:
        """把容器类型写入文件栈顶。

        参数:
            container: 新进入的数组或对象。

        返回:
            无返回值。

        异常:
            临时文件写入失败时传播原始 OSError。
        """
        self._stack_file.seek(self._stack_depth)
        self._stack_file.write(b"[" if container is _Container.ARRAY else b"{")
        self._stack_depth += 1

    def _stack_peek(self) -> _Container:
        """读取文件栈顶容器类型。

        参数:
            无。

        返回:
            当前最内层容器。

        异常:
            栈为空或临时文件内容损坏时抛出 JsonStreamValidationError。
        """
        if self._stack_depth <= 0:
            self._invalid()
        self._stack_file.seek(self._stack_depth - 1)
        marker = self._stack_file.read(1)
        if marker == b"[":
            return _Container.ARRAY
        if marker == b"{":
            return _Container.OBJECT
        self._invalid()

    def _top_is(self, container: _Container) -> bool:
        """判断当前文件栈顶是否为指定容器。

        参数:
            container: 期望的容器类型。

        返回:
            True 表示栈顶类型一致。

        异常:
            栈为空时抛出 JsonStreamValidationError。
        """
        return self._stack_peek() is container

    def _invalid(self) -> NoReturn:
        """抛出统一的 JSON 语法验证异常。

        参数:
            无。

        返回:
            该函数不返回。

        异常:
            总是抛出 JsonStreamValidationError。
        """
        raise JsonStreamValidationError()


def validate_json_stream(
    stream: BinaryIO,
    *,
    cancellation: threading.Event | None = None,
) -> None:
    """流式验证 JSON 语法，不构造完整标量或递归解析容器。

    参数:
        stream: 以二进制方式读取的 JSON 文件或等价流。
        cancellation: 可选的线程取消事件；每次读取及每处理 4096 个字符都会检查。

    返回:
        None 表示整个流的 UTF-8 编码和 JSON 语法均合法。

    异常:
        JSON 编码或语法非法时抛出 JsonStreamValidationError；取消事件已设置时
        抛出 JsonStreamValidationCancelled；底层流读取失败时原样传播 OSError。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    with tempfile.TemporaryFile() as stack_file:
        parser = _JsonSyntaxParser(stack_file, cancellation)
        while True:
            parser.check_cancellation()
            chunk = stream.read(_READ_SIZE)
            parser.check_cancellation()
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise TypeError("json_stream_binary_required")
            try:
                text = decoder.decode(chunk, final=False)
            except UnicodeDecodeError as error:
                raise JsonStreamValidationError("json_stream_invalid_utf8") from error
            parser.feed(text)

        try:
            final_text = decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise JsonStreamValidationError("json_stream_invalid_utf8") from error
        parser.feed(final_text)
        parser.finish()


__all__ = [
    "JsonStreamValidationCancelled",
    "JsonStreamValidationError",
    "validate_json_stream",
]
