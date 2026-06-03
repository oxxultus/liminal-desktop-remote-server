
# Liminal Remote MCP Server - 개발 및 확장 규격 가이드

본 가이드는 Liminal 클라이언트(Remote HTTP MCP 플러그인)와 완벽히 호환되도록 설계된 원격 도구(Tool) 서버의 아키텍처 및 새로운 도구 작성 법식을 명시합니다. 
Liminal이 원격 API를 통해 도구 목록을 조회(`GET`)하고, 특정 도구를 실행(`POST`)하는 규격을 충족하기 위해 본 서버는 고정된 라우터 구조와 Pydantic 스키마 체계를 엄격히 준수해야 합니다.

---

## 핵심 아키텍처 및 Liminal 연동 규격

Liminal Remote MCP 플러그인은 서버에 접속할 때 딱 **두 가지 엔드포인트**만 바라보고 통신합니다. 코드 수정 시 이 라우팅 주소와 입출력 JSON 구조는 절대 변경해서는 안 됩니다.

### 1. 도구 검색 규격 (`GET /api/v1/tools`)
* **역할**: Liminal 내부 LLM이 인식할 수 있도록 현재 서버가 지원하는 모든 도구의 '이름', '설명', '인자값 규격'을 반환합니다.
* **통신 스펙**: `AVAILABLE_TOOLS`에 정의된 딕셔너리를 기반으로 `ToolsListResponse` 형태로 래핑하여 리턴합니다.

### 2. 도구 호출 규격 (`POST /api/v1/tools/execute`)
* **역할**: 사용자의 프롬프트를 분석한 LLM이 특정 도구를 쓰겠다고 결정하면, 인자값을 채워 이 엔드포인트로 요청을 보냅니다.
* **통신 스펙**: `ToolExecuteRequest` 규격으로 들어온 `arguments`를 파싱하여 내부 비즈니스 로직을 수행한 뒤, 최종 결과를 **`{"content": [{"type": "text", "text": "결과문자열"}]}`** 포맷(`ToolExecuteResponse`)으로 반환해야 LLM이 정상적으로 인식합니다.

---

## 신규 커스텀 도구(Tool) 작성 및 추가 방법 (Step-by-Step)

새로운 도구(예: 특정 포트 프로세스를 닫는 도구 등)를 추가하고 싶다면, 소스 코드 내에서 딱 **두 곳**만 순서대로 수정하면 됩니다.

### Step 1: `AVAILABLE_TOOLS` 명세 등록 (JSON Schema 정의)
먼저 LLM이 도구의 존재를 알고 인자값을 올바르게 채울 수 있도록 `AVAILABLE_TOOLS` 딕셔너리에 명세를 추가합니다. `inputSchema`는 표준 JSON Schema 규격을 따릅니다.

* **예시 (특정 포트를 닫는 도구 추가 시)**:
```python
AVAILABLE_TOOLS = {
    # ... 기존 도구들 ...
    
    "kill_port_process": {
        "description": "서버에서 특정 네트워크 포트를 점유하고 있는 프로세스를 찾아 강제로 종료합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {
                    "type": "integer", 
                    "description": "닫으려고 하는 대상 네트워크 포트 번호 (예: 8080)"
                }
            },
            "required": ["port"]  # 필수 인자값 명시
        }
    }
}

```

### Step 2: `execute_mcp_tool` 라우터 내부에 실행 로직 라우팅

Liminal이 보낸 도구 이름(`tool_name`)을 식별하여 실제 파이썬 코드가 동작하도록 `if/elif` 절을 추가하고, `args`에서 매개변수를 꺼내 처리합니다.

```python
@app.post("/api/v1/tools/execute", response_model=ToolExecuteResponse, dependencies=[Depends(verify_api_key)])
async def execute_mcp_tool(request: ToolExecuteRequest):
    tool_name = request.name
    args = request.arguments

    # ... 앞선 에러 가드레일 조건문들 ...

    try:
        if tool_name == "get_server_status":
            # ... 기존 로직 ...
            pass

        # 새로운 도구 비즈니스 로직 분기 추가
        elif tool_name == "kill_port_process":
            target_port = int(args.get("port"))
            
            # 실제 포트 프로세스 종료 로직 수행
            import psutil
            killed_pids = []
            for conn in psutil.net_connections():
                if conn.laddr.port == target_port and conn.pid:
                    try:
                        proc = psutil.Process(conn.pid)
                        proc.terminate()  # 혹은 proc.kill()
                        killed_pids.append(conn.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            
            # Liminal 반환 표준 규격 정의 (반드시 content -> type, text 구조 유지)
            if killed_pids:
                res_text = f"성공: {target_port}번 포트를 사용 중인 프로세스(PID: {killed_pids})를 종료했습니다."
            else:
                res_text = f"ℹ알림: {target_port}번 포트를 점유 중인 활성 프로세스가 없습니다."
                
            return {"content": [{"type": "text", "text": res_text}]}

    except Exception as biz_error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"도구 실행 중 에러 발생: {str(biz_error)}"
        )

```

---

## 보안 및 헤더 규격 (X-API-KEY)

Liminal 클라이언트에서 원격 서버를 등록할 때 반드시 커스텀 헤더 설정을 동반해야 합니다.

* **Key**: `X-API-KEY`
* **Value**: 서버 가동 환경 시스템에 설정된 `MCP_SERVER_KEY` 값

인증에 실패할 경우 Liminal 측에는 `401 Unauthorized` 에러가 반환되며, 도구 연동이 차단됩니다.

---

## 확장 시 주의사항 (가드레일)

1. **동기 블로킹 방지**: 외부 API 연동이나 시간이 오래 걸리는 작업(I/O BOUND)을 도구로 추가할 경우, `execute_mcp_tool` 함수가 `async`로 정의되어 있으므로 가급적 `await` 가능한 라이브러리(예: `httpx`)를 사용하거나 `asyncio.to_thread`로 감싸서 실행하십시오.
2. **리턴 타입 엄수**: 최종 `return` 문은 항상 `{"content": [{"type": "text", "text": "결과 메세지"}]}` 형태여야 합니다. 딕셔너리나 리스트 생과를 그대로 던지면 Pydantic 검증 에러(`ValidationError`)가 발생합니다.