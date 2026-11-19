import { Link, NavLink } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";

export function PersonalSkillsSidebar() {
  return (
    <div className="sidebar-scroll">
      <section className="sidebar-section" aria-labelledby="skills-nav-title">
        <div className="sidebar-section-heading">
          <span id="skills-nav-title">个人能力</span>
        </div>
        <nav className="sidebar-links" aria-label="个人技能导航">
          <NavLink
            className={({ isActive }) =>
              `sidebar-link${isActive ? " is-active" : ""}`
            }
            end
            to="/skills"
          >
            <Icon name="spark" size={16} />
            <span>我的技能</span>
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              `sidebar-link${isActive ? " is-active" : ""}`
            }
            to="/chat"
          >
            <Icon name="chat" size={16} />
            <span>返回对话</span>
          </NavLink>
        </nav>
      </section>

      <div className="sidebar-guidance">
        <Icon name="lock" size={17} />
        <p>
          个人技能只对你本人可见。新保存会启用当前版本，并同步到下一次对话的可用目录。
        </p>
      </div>
    </div>
  );
}
