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
  skill_guide_invalid: "无法从 SKILL.md 读取操作指南。",
  skill_guide_too_large: "Skill 指南超过 100,000 个字符上限。",
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
    "配置已被其他管理员修改。旧草稿未覆盖新配置，请重新核对最新详情。",
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
  skill_parameter_schema_unsupported:
    "技能脚本参数 Schema 使用了当前平台不支持的规则。",
  skill_parameter_schema_invalid:
    "技能脚本参数 Schema 不是受支持的对象结构。",
  skill_parameters_invalid: "技能脚本参数不符合登记结构。",
  idempotency_key_invalid: "技能包请求标识无效，请重新选择文件后再试。",
  idempotency_conflict:
    "同一请求标识已用于不同技能包，请重新选择文件后再试。",
  file_storage_unavailable: "技能包存储暂时不可用，请稍后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次技能请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的技能响应。",
};

export function skillErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const message =
      SKILL_ERROR_COPY[error.detail] ?? "技能操作未完成，请稍后重试。";
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
