import { ApiError } from "../../shared/api/client";

const SKILL_ERROR_COPY: Record<string, string> = {
  skill_package_invalid: "技能包结构或 ZIP 内容不符合要求。",
  skill_zip_invalid: "无法读取 ZIP 技能包。",
  skill_zip_unreadable: "ZIP 已损坏或不完整。",
  skill_zip_encrypted: "不支持加密 ZIP，请重新导出未加密技能包。",
  skill_zip_too_large: "ZIP 文件超过 25 MiB 上限。",
  skill_zip_ratio_exceeded:
    "ZIP 压缩比异常，解压内容可能超过平台安全边界。",
  skill_zip_ratio_invalid: "ZIP 压缩比信息异常，无法安全展开技能包。",
  skill_zip_size_mismatch: "ZIP 实际解压大小与目录信息不一致。",
  skill_zip_path_invalid:
    "技能包包含不受支持的文件位置或目录规则。请将文件移动到 scripts/、templates/ 或 references/ 等受支持位置后重试。",
  skill_zip_path_duplicate: "ZIP 包含重复或规范化后冲突的文件路径。",
  skill_zip_special_file: "技能包不能包含符号链接、硬链接或设备项。",
  skill_package_too_large: "解压后总量超过 100 MiB 上限。",
  skill_package_file_count_exceeded: "技能包文件数超过 500 个上限。",
  skill_package_integrity_invalid:
    "技能包持久化内容与清单不一致，请重新导入完整 ZIP。",
  skill_file_too_large: "技能包内单个文件超过 25 MiB 上限。",
  skill_entry_missing: "技能包根目录必须包含 SKILL.md。",
  skill_metadata_missing: "SKILL.md 缺少 YAML 元数据。",
  skill_metadata_invalid: "SKILL.md 元数据不完整或字段不合法。",
  skill_guide_encoding_invalid: "SKILL.md 必须使用 UTF-8 编码。",
  skill_guide_invalid:
    "Markdown 正文不能为空、不能超过 100,000 个字符，也不能包含控制字符。",
  skill_guide_too_large: "Skill 指南超过 100,000 个字符上限。",
  skill_markdown_filename_invalid:
    "只支持 .md 文件，扩展名不区分大小写。",
  skill_markdown_unreadable: "无法读取 Markdown 文件，请重新选择。",
  skill_markdown_too_large: "Markdown 文件不能超过 1 MiB。",
  skill_markdown_empty: "Markdown 文件不能为空。",
  skill_markdown_encoding_invalid: "Markdown 文件必须使用 UTF-8 编码。",
  skill_markdown_metadata_invalid:
    "Markdown 文件开头的 YAML 元数据格式不正确。",
  skill_markdown_field_unsupported:
    "Markdown 元数据包含不支持的字段，仅允许 name、description 和 slug。",
  skill_markdown_slug_invalid: "Markdown 元数据中的 slug 必须是文本。",
  skill_markdown_name_invalid: "Markdown 元数据中的 name 不合法。",
  skill_markdown_description_invalid:
    "Markdown 元数据中的 description 不合法。",
  skill_markdown_control_character:
    "Markdown 文件不能包含空字节或其他不支持的控制字符。",
  skill_name_invalid:
    "技能名称不能为空、不能超过 120 个字符，也不能包含控制字符。",
  skill_description_invalid:
    "技能描述不能为空、不能超过 1,000 个字符，也不能包含控制字符。",
  skill_guide_control_character:
    "Markdown 正文不能包含空字节或其他不支持的控制字符。",
  skill_personal_create_conflict:
    "个人技能创建发生并发冲突，请刷新目录后重试。",
  skill_personal_publish_conflict:
    "个人技能版本发布发生并发冲突，请刷新最新内容后重试。",
  skill_personal_integrity_invalid:
    "个人技能数据不完整，当前操作未执行，请刷新后重试。",
  // 正常正文远低于原始请求体上限，413 通常表示请求体异常膨胀而非业务字段超限。
  skill_personal_request_too_large:
    "个人技能请求内容过大。正常 100,000 字符正文不应触发；请求体上限为 2 MiB，请刷新后重试。",
  personal_skill_file_invalid: "个人技能必须使用有效的 Markdown 文件。",
  personal_skill_file_type_invalid:
    "只支持 .md 文件，扩展名不区分大小写。",
  personal_skill_file_empty: "Markdown 文件不能为空。",
  personal_skill_file_too_large: "Markdown 文件不能超过 1 MiB。",
  personal_skill_name_invalid: "个人技能名称不合法。",
  personal_skill_description_invalid: "个人技能描述不合法。",
  personal_skill_guide_invalid: "个人技能 Markdown 正文不合法。",
  skill_scripts_invalid: "脚本登记、路径或参数结构不符合技能协议。",
  skill_dependency_invalid: "依赖声明不是合法的 PEP 508 需求。",
  skill_dependencies_too_many: "技能声明的 Python 依赖数量超过上限。",
  skill_dependency_url_unsupported:
    "技能不能声明 URL、Git 或本地路径依赖。",
  skill_dependency_extras_unsupported:
    "当前版本不支持依赖 extras 声明。",
  skill_dependency_unsatisfied:
    "当前 Python 环境缺少所需依赖或版本不符，不能启用该版本。",
  skill_slug_exists: "该 slug 已存在。首发导入不能覆盖已有技能。",
  skill_slug_mismatch: "更新包中的 slug 与目标技能不一致，版本未发布。",
  skill_revision_conflict:
    "内容已被另一个页面更新。当前草稿没有覆盖最新内容，请刷新后重新核对。",
  skill_catalog_integrity_invalid:
    "技能目录数据不完整或相互冲突，请刷新后重试。",
  skill_execution_request_conflict:
    "同一技能执行请求已被其他内容占用，请刷新后重新发起。",
  skill_version_conflict: "该技能版本已存在或发生并发冲突。",
  skill_selection_unavailable:
    "所选技能已停用或版本不可用，请重新选择后发送。",
  skill_selection_invalid: "技能选择重复或包含当前 Run 不支持的技能。",
  skill_not_found: "技能或版本不存在，或已不再可用。",
  skill_version_not_found: "技能版本不存在或不属于目标技能。",
  company_skill_delete_unsupported:
    "全公司技能不支持删除，如需下线请将其停用。",
  visibility_scope_invalid: "技能可见范围无效，请选择全公司或部门共享。",
  visibility_scope_unsupported: "当前操作不支持所选可见范围。",
  owner_department_id_required: "部门技能必须指定归属部门。",
  owner_department_required: "部门技能必须指定归属部门。",
  owner_department_not_found: "所选归属部门不存在或已归档。",
  owner_department_not_allowed: "全公司技能不能指定归属部门。",
  visibility_grants_invalid: "可见性授权配置无效，请检查部门或角色设置。",
  visibility_grants_not_allowed: "当前可见范围不支持额外共享授权。",
  admin_required: "你没有权限执行该技能管理操作。",
  permission_denied: "你没有权限执行该技能操作。",
  skill_parameter_schema_unsupported:
    "技能脚本参数 Schema 使用了当前平台不支持的规则。",
  skill_parameter_schema_invalid:
    "技能脚本参数 Schema 不是受支持的对象结构。",
  skill_parameters_invalid: "技能脚本参数不符合登记结构。",
  idempotency_key_invalid: "技能请求标识无效，请重新发起操作。",
  idempotency_conflict:
    "同一请求标识已用于不同内容，请重新发起操作。",
  file_storage_unavailable: "技能包存储暂时不可用，请稍后重试。",
  database_unavailable: "数据服务暂时不可用，请稍后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次技能请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的技能响应。",
};

function lookupSkillErrorCopy(code: string): string | undefined {
  // 严格通过 Object.hasOwn 查表，防止原型属性污染导致异常输出。
  return Object.hasOwn(SKILL_ERROR_COPY, code)
    ? SKILL_ERROR_COPY[code]
    : undefined;
}

export function skillErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const message =
      lookupSkillErrorCopy(error.detail) ?? "技能操作未完成，请稍后重试。";
    return error.memberPath
      ? `${message}\n包内文件：${error.memberPath}`
      : message;
  }
  return "技能操作未完成，请稍后重试。";
}

export function isSkillRevisionConflict(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    (error.detail === "skill_revision_conflict" ||
      error.detail === "skill_conflict")
  );
}
