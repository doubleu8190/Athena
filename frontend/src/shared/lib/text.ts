/**
 * 零分配统计字符串的 Unicode 码点数量，语义与 `Array.from(value).length` 完全等价：
 * - 合法 UTF-16 代理对（高代理 0xD800..0xDBFF 紧跟低代理 0xDC00..0xDFFF）合算为 1 个码点；
 * - 普通 BMP 字符与任意孤立高/低代理码元各算 1 个码点；
 * - 全程仅做整型码元扫描，不分配临时字符数组，适用于编辑器与表单大文本高频校验。
 */
export function countCodePoints(value: string): number {
  let count = 0;
  const length = value.length;
  for (let index = 0; index < length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff && index + 1 < length) {
      const next = value.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        index += 1;
      }
    }
    count += 1;
  }
  return count;
}
