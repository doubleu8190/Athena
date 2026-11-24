import { Link } from "react-router-dom";

import { Button, EmptyState } from "../shared/ui";

export function AccessDeniedPage() {
  return (
    <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-6 py-12">
      <EmptyState
        action={
          <Button asChild size="sm" variant="secondary">
            <Link to="/chat">返回对话工作台</Link>
          </Button>
        }
        description="当前页面仅对具备对应管理权限的企业管理员开放，你仍可正常使用工作台的其他功能。"
        icon="shield"
        title="当前账号没有管理员权限"
      />
    </div>
  );
}
