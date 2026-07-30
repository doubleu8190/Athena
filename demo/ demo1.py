import asyncio
import json
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Optional
import uvicorn

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 连接管理 ----------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}  # session_id -> websocket

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket

    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]

    async def send_to_session(self, session_id: str, data: dict):
        ws = self.active_connections.get(session_id)
        if ws:
            await ws.send_text(json.dumps(data))

manager = ConnectionManager()

# ---------- 审批状态管理 ----------
# 存储每个待审批操作的状态
pending_approvals: Dict[str, asyncio.Future] = {}

# ---------- 模拟 Agent 执行流程 ----------
async def execute_tool_with_approval(session_id: str, tool_name: str, params: dict):
    """模拟执行一个需要审批的工具"""
    if tool_name != "delete_file":
        # 非敏感工具直接执行
        return {"status": "success", "result": f"Executed {tool_name}"}

    # 1. 生成审批请求ID
    approval_id = str(uuid.uuid4())

    # 2. 发送审批请求给前端
    approval_request = {
        "type": "approval_request",
        "approval_id": approval_id,
        "tool": tool_name,
        "params": params,
        "title": "⚠️ 确认删除文件",
        "description": f"您确定要永久删除文件 **{params.get('path')}** 吗？此操作不可恢复！",
        "timeout": 60,  # 秒
    }
    await manager.send_to_session(session_id, approval_request)

    # 3. 创建一个 Future 用于等待用户响应
    future = asyncio.Future()
    pending_approvals[approval_id] = future

    try:
        # 设置超时
        result = await asyncio.wait_for(future, timeout=60)
        if result.get("action") == "allow":
            # 用户批准，执行实际工具
            # 这里模拟执行删除
            await asyncio.sleep(1)  # 模拟耗时操作
            return {"status": "success", "result": f"Deleted {params['path']}"}
        else:
            return {"status": "cancelled", "result": "User denied the operation"}
    except asyncio.TimeoutError:
        return {"status": "timeout", "result": "Approval request timed out"}
    finally:
        # 清理
        pending_approvals.pop(approval_id, None)

# ---------- WebSocket 端点 ----------
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            # 处理用户响应（批准/拒绝）
            if msg.get("type") == "approval_response":
                approval_id = msg.get("approval_id")
                action = msg.get("action")  # "allow" 或 "deny"
                future = pending_approvals.get(approval_id)
                if future and not future.done():
                    future.set_result({"action": action})
                continue

            # 处理用户发送的指令（模拟 Agent 触发工具调用）
            if msg.get("type") == "user_command":
                command = msg.get("command")
                if command == "delete_file":
                    # 模拟 Agent 决定调用 delete_file 工具
                    result = await execute_tool_with_approval(
                        session_id,
                        "delete_file",
                        {"path": msg.get("file_path", "/tmp/test.txt")}
                    )
                    # 向用户反馈执行结果
                    await manager.send_to_session(session_id, {
                        "type": "tool_result",
                        "data": result
                    })
    except WebSocketDisconnect:
        manager.disconnect(session_id)

# ---------- 启动服务 ----------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)