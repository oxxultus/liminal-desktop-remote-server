import os
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Header, HTTPException, Depends, status
from pydantic import BaseModel, Field
import psutil

# 💡 클라이언트에서 넘겨줄 Security Token 정의 (실무에선 .env 또는 OS 환경변수 관리 권장)
VALID_API_KEY = os.getenv("MCP_SERVER_KEY", "your-secret-token-here")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 애플리케이션 시작 시 필요한 초기화 로직이 있다면 여기에 작성합니다.
    print("원격 인프라 MCP 서버 가동 시작...")
    yield
    print("원격 인프라 MCP 서버 종료 중...")

# FastAPI 앱 정의 및 lifespan 등록
app = FastAPI(title="Remote Infra MCP Server", version="1.0.0", lifespan=lifespan)

# ── 시큐리티 디펜던시 (X-API-KEY 인증) ──
async def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-KEY")):
    if not x_api_key or x_api_key != VALID_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 누락된 보안 토큰입니다."
        )
    return x_api_key

# ── Pydantic 스키마 모델 구조체 ──
class ToolSpecification(BaseModel):
    name: str = Field(..., description="도구 고유 식별자")
    description: str = Field(..., description="LLM 주입용 도구 설명 설명문")
    inputSchema: Dict[str, Any] = Field(..., description="도구 인자값 규격 파라미터 (JSON Schema 표준 규격)")

class ToolsListResponse(BaseModel):
    tools: List[ToolSpecification]

class ToolExecuteRequest(BaseModel):
    name: str = Field(..., description="실행할 도구 이름")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="LLM이 채워준 인자 맵")

class ToolResultContent(BaseModel):
    type: str = "text"
    text: str

class ToolExecuteResponse(BaseModel):
    content: List[ToolResultContent]

# ── MCP 도구 명세 보관소 ──
AVAILABLE_TOOLS = {
    "get_server_status": {
        "description": "원격 서버의 실제 실시간 CPU 및 메모리 상태 요약을 수급합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_disk": {"type": "boolean", "description": "디스크 정보(잔여 공간) 포함 여부"}
            },
            "required": []
        }
    },
    "get_process_list": {
        "description": "서버에서 상위 CPU를 많이 점유 중인 프로세스 목록을 조회합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "가져올 프로세스 개수 (기본값 5)"}
            },
            "required": []
        }
    }
}

# ── 라우터 엔드포인트 구현 ──

@app.get("/api/v1/tools", response_model=ToolsListResponse, dependencies=[Depends(verify_api_key)])
async def list_mcp_tools():
    """서버가 제공 가능한 도구 목록을 클라이언트에 제공합니다."""
    tools_payload = []
    for name, spec in AVAILABLE_TOOLS.items():
        tools_payload.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"]
        })
    return {"tools": tools_payload}


@app.post("/api/v1/tools/execute", response_model=ToolExecuteResponse, dependencies=[Depends(verify_api_key)])
async def execute_mcp_tool(request: ToolExecuteRequest):
    """LLM이 선택한 도구를 매개변수와 함께 실행합니다."""
    tool_name = request.name
    args = request.arguments

    if tool_name not in AVAILABLE_TOOLS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"요청한 도구('{tool_name}')는 원격 서버에 등록되어 있지 않습니다."
        )

    try:
        # 1. 서버 상태 조회 도구
        if tool_name == "get_server_status":
            cpu_usage = psutil.cpu_percent(interval=0.1)
            memory_usage = psutil.virtual_memory().percent
            status_text = f"원격 인프라 가동 정상 (현재 CPU: {cpu_usage}%, MEM: {memory_usage}%)"

            include_disk = args.get("include_disk", False)
            if str(include_disk).lower() == "true":
                disk_info = psutil.disk_usage('/')
                free_disk_gb = round(disk_info.free / (1024 ** 3), 1)
                status_text += f" / 메인 디스크 공간 잔여: {free_disk_gb}GB"

            return {"content": [{"type": "text", "text": status_text}]}

        # 2. 고점유 프로세스 조회 도구
        elif tool_name == "get_process_list":
            limit = int(args.get("limit", 5))
            processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent']):
                try:
                    processes.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            # CPU 사용량 높은 순 정렬
            processes = sorted(processes, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:limit]
            
            result_payload = "📋 [CPU 고점유 프로세스 목록]\n"
            for p in processes:
                result_payload += f"- PID {p['pid']}: {p['name']} ({p['cpu_percent']}%)\n"
                
            return {"content": [{"type": "text", "text": result_payload.strip()}]}

    except Exception as biz_error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"도구 내부 가동 중 에러 발생: {str(biz_error)}"
        )