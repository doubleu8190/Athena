import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { usePublicSkills } from "./hooks";
import { skillErrorMessage } from "./errorMessages";
import { skillScope } from "./model";
import { SkillScopeBadge } from "./SkillScopeBadge";

interface SkillCatalogPickerProps {
  disabled?: boolean;
  onChange: (skillIds: string[]) => void;
  selectedSkillIds: readonly string[];
}

export function SkillCatalogPicker({
  disabled = false,
  onChange,
  selectedSkillIds,
}: SkillCatalogPickerProps) {
  const skills = usePublicSkills();
  const selected = new Set(selectedSkillIds);
  const availableIds = new Set((skills.data ?? []).map((skill) => skill.id));
  const unavailableSelectedIds = selectedSkillIds.filter(
    (skillId) => skills.isSuccess && !availableIds.has(skillId),
  );

  const toggle = (skillId: string) => {
    const next = new Set(selectedSkillIds);
    if (next.has(skillId)) {
      next.delete(skillId);
    } else {
      next.add(skillId);
    }
    onChange(Array.from(next));
  };

  return (
    <section
      aria-labelledby="chat-skill-picker-title"
      className="skill-catalog-picker"
    >
      <header>
        <div>
          <span className="empty-panel-kicker">Skills</span>
          <h2 id="chat-skill-picker-title">指定本次任务技能</h2>
        </div>
        <span className="skill-catalog-count">
          {selectedSkillIds.length > 0
            ? `已选 ${selectedSkillIds.length} 项`
            : "可由 Agent 自动选择"}
        </span>
      </header>
      <p className="skill-catalog-help">
        手动选择只固定本次新 Run 的方法与版本；留空时 Agent 仍可根据任务自动加载已启用技能。
      </p>

      {skills.isPending ? (
        <p className="skill-catalog-state" role="status">
          正在读取已启用技能…
        </p>
      ) : skills.isError ? (
        <ErrorBanner
          message={skillErrorMessage(skills.error)}
          onRetry={() => void skills.refetch()}
        />
      ) : skills.data.length === 0 ? (
        <p className="skill-catalog-state">
          管理员尚未发布可用技能。当前消息仍可按普通任务提交。
        </p>
      ) : (
        <fieldset className="skill-catalog-options" disabled={disabled}>
          <legend className="sr-only">选择技能</legend>
          {skills.data.map((skill) => {
            const scope = skillScope(skill);
            return (
              <label
                className={`skill-catalog-option is-${scope}${
                  selected.has(skill.id) ? " is-selected" : ""
                }`}
                key={skill.id}
              >
                <input
                  checked={selected.has(skill.id)}
                  onChange={() => toggle(skill.id)}
                  type="checkbox"
                />
                <span className="skill-catalog-option-main">
                  <strong>{skill.name}</strong>
                  <small>
                    <SkillScopeBadge compact scope={scope} />
                    <span>
                      {skill.current_version_label ?? "当前版本"}
                    </span>
                    <span aria-hidden="true">·</span>
                    <span>
                      {scope === "personal"
                        ? "纯文档"
                        : skill.script_requires_approval
                          ? "脚本执行需审批"
                          : "脚本执行无需审批"}
                    </span>
                  </small>
                  <span>{skill.description}</span>
                </span>
                <Icon name="spark" size={15} />
              </label>
            );
          })}
        </fieldset>
      )}

      {unavailableSelectedIds.length > 0 ? (
        <div className="skill-catalog-conflict" role="alert">
          <Icon name="shield" size={16} />
          <div>
            <strong>有已选技能不再可用</strong>
            <p>
              服务端最新目录中找不到 {unavailableSelectedIds.length} 个已选项。页面不会自动替换，请重新选择或发送时按冲突提示重试。
            </p>
            <button
              className="text-button"
              onClick={() =>
                onChange(
                  selectedSkillIds.filter((skillId) =>
                    availableIds.has(skillId),
                  ),
                )
              }
              type="button"
            >
              移除不可用项
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
