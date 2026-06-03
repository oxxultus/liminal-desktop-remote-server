import os
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Header, HTTPException, Depends, status
from pydantic import BaseModel, Field

import zigpy.state
import zigpy.types as t
from zigpy.exceptions import NetworkNotFormed
from zigpy_znp.zigbee.application import ControllerApplication

# 💡 클라이언트에서 넘겨줄 Security Token 정의 (실무에선 .env 등으로 관리 권장)
VALID_API_KEY = "your-secret-token-here"


# ── 1. 지그비 실시간 상태 캐싱 핸들러 정의 ──
class FastAPIZigbeeHandler:
    """지그비 네트워크에서 발생하는 이벤트를 실시간으로 메모리에 캐싱하는 핸들러"""
    def __init__(self, application):
        self.application = application
        self.latest_states = {}

    def device_joined(self, device):
        ieee_str = str(device.ieee)
        print(f"📡 [Zigbee] 새 장치 페어링 완료! IEEE: {ieee_str}")
        if ieee_str not in self.latest_states:
            self.latest_states[ieee_str] = {"status": "joined", "last_seen": "방금 전"}

    def attribute_updated(self, device, cluster, attribute_id, value):
        """센서나 스위치 상태 변경 시 실시간 매핑"""
        ieee_str = str(device.ieee)
        print(f"📝 [Zigbee] 데이터 수신 [{ieee_str}]: Cluster {cluster.id} -> Value {value}")
        
        if ieee_str not in self.latest_states:
            self.latest_states[ieee_str] = {}
        
        # 클러스터 ID별 표준 매핑 예시
        if cluster.id == 1026:   # 온습도 센서 (Temperature)
            self.latest_states[ieee_str]["temperature"] = value / 100.0
        elif cluster.id == 6:    # 스마트 플러그/스위치 (On/Off)
            self.latest_states[ieee_str]["state"] = "ON" if value else "OFF"
        
        self.latest_states[ieee_str]["last_seen"] = "최근 업데이트됨"


# 글로벌 핸들러 참조용 변수 선언
zigbee_handler = None


# ── 2. FastAPI Lifespan (NVRAM 초기화 가드레일 반영) ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    global zigbee_handler
    
    config = {
        "database_path": "zigbee_devices.db",
        "device": {
            "path": "/dev/ttyUSB0",
            "baudrate": 115200
        },
        "ota": {
            "otau_directory": None,
            "extra_providers": []
        }
    }
    
    print("🚀 지그비 동글 초기화 시작...")
    try:
        # ✅ raw config 전달
        z2m_app = ControllerApplication(config=config)
        
        await z2m_app.connect()
        
        try:
            await z2m_app.load_network_info()
            print("✅ 기존 네트워크 정보 로드 성공")
        except NetworkNotFormed:
            print("⚠️ 네트워크 미형성 또는 데이터 손상 감지 → 공식 API 기반 네트워크 리셋 및 신규 생성 중...")
            
            # 1. zigpy 상위 공식 API를 활용하여 기존 지그비 스택 주소와 NVRAM 맵을 공장 초기화
            await z2m_app.reset_network_info()
            
            # 2. 신규 네트워크 구축을 위한 구조체 선언
            from zigpy.state import NetworkInfo, NodeInfo, Key
            
            network_info = NetworkInfo()
            network_info.channel = 11
            network_info.channel_mask = t.Channels.from_channel_list([11])
            network_info.pan_id = t.PanId(int.from_bytes(os.urandom(2), "big") & 0x3FFF)
            network_info.extended_pan_id = t.ExtendedPanId.deserialize(os.urandom(8))[0]
            network_info.network_key = Key(key=t.KeyData(os.urandom(16)), seq=0, tx_counter=0)
            network_info.tc_link_key = Key(key=t.KeyData(b"ZigBeeAlliance09"), seq=0, tx_counter=0)
            
            node_info = NodeInfo()
            node_info.nwk = t.NWK(0x0000)
            node_info.ieee = t.EUI64.deserialize(os.urandom(8))[0]
            node_info.logical_type = 0  # 0 = Coordinator 상수를 뜻함 (버전 독립적 코드)
            
            # 3. 꼬인 데이터가 삭제된 보드판 위에 완전히 새로운 토폴로지 정보 라이팅
            try:
                await z2m_app.write_network_info(network_info=network_info, node_info=node_info)
            except Exception:
                # 만약 라이팅 직후 즉각 칩셋 꼬임이 발견될 경우를 대비한 칩셋 SYS_RESET 예외 복구 루틴
                if hasattr(z2m_app._znp, "request"):
                    await z2m_app._znp.request(0x21, 0x00, b"\x01")
                    await asyncio.sleep(1)
                await z2m_app.write_network_info(network_info=network_info, node_info=node_info)
                
            print("✅ 신규 가상 Zigbee 토폴로지 네트워크 형성 완료")
            
            # 4. 새로 기입된 정보를 정상적으로 인스턴스에 로드
            await z2m_app.load_network_info()
        
        # 지그비 라디오 네트워크 가동
        await z2m_app.start_network()
        
        zigbee_handler = FastAPIZigbeeHandler(z2m_app)
        z2m_app.add_listener(zigbee_handler)
        app.state.zigbee = z2m_app
        print("🟢 지그비 코디네이터 가동 성공!")

    except Exception as e:
        import traceback
        print(f"❌ 지그비 동글 구동 실패: {e}")
        traceback.print_exc()
        app.state.zigbee = None

    yield

    if app.state.zigbee:
        print("🛑 지그비 코디네이터 종료 중...")
        await app.state.zigbee.shutdown()


# FastAPI 앱 정의 및 lifespan 등록
app = FastAPI(title="Liminal Remote MCP Server", version="1.0.0", lifespan=lifespan)


# ── 3. 시큐리티 디펜던시 (X-API-KEY 인증) ──
async def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-KEY")):
    if not x_api_key or x_api_key != VALID_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="❌ 유효하지 않거나 누락된 보안 토큰입니다."
        )
    return x_api_key


# ── 4. Pydantic 스키마 모델 구조체 ──
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


# ── 5. MCP 도구 명세 보관소 ──
AVAILABLE_TOOLS = {
    "get_server_status": {
        "description": "원격 인프라 서버의 현재 CPU 및 메모리 상태 요약을 수급합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_disk": {"type": "boolean", "description": "디스크 정보 포함 여부"}
            },
            "required": []
        }
    },
    "fetch_remote_data": {
        "description": "지정한 외부 데이터 식별자로부터 메타데이터 로그를 가져옵니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data_id": {"type": "string", "description": "조회할 유일 데이터 키값"}
            },
            "required": ["data_id"]
        }
    },
    "zigbee_permit_join": {
        "description": "새로운 지그비 장치(센서, 스위치, 버튼)를 무선 네트워크망에 페어링할 수 있도록 개방합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "duration": {"type": "integer", "description": "개방 상태를 유지할 시간(초 단위, 기본값 60초)"}
            },
            "required": []
        }
    },
    "zigbee_list_devices": {
        "description": "현재 지그비 동글망에 페어링되어 등록된 기기 목록 및 수신된 수치 데이터 상태를 확인합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "zigbee_control_switch": {
        "description": "지그비 기반 스마트 조명, 콘센트, 플러그 등 액츄에이터 기기를 켜거나(ON) 끕니다(OFF).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ieee_address": {"type": "string", "description": "제어할 기기의 고유 IEEE 주소 식별값 (예: 00:12:4b:00...)"},
                "state": {"type": "string", "enum": ["ON", "OFF"], "description": "변경할 스위치 전원 명령 명세"}
            },
            "required": ["ieee_address", "state"]
        }
    }
}


# ── 6. 라우터 엔드포인트 구현 ──

@app.get("/api/v1/tools", response_model=ToolsListResponse, dependencies=[Depends(verify_api_key)])
async def list_mcp_tools():
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
    tool_name = request.name
    args = request.arguments

    if tool_name not in AVAILABLE_TOOLS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"요청한 도구('{tool_name}')는 원격 서버에 등록되어 있지 않습니다."
        )

    if "zigbee" in tool_name and not app.state.zigbee:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="⚠️ 로컬 지그비 어댑터 하드웨어가 정상 구동되지 않은 상태입니다."
        )

    try:
        if tool_name == "get_server_status":
            import psutil

            # 1. CPU 및 메모리 실제 수치 계산
            # percpu=False로 전체 평균 사용률을 가져옵니다. 
            # interval=0.1을 주어 순간적인 측정 오차를 줄입니다.
            cpu_usage = psutil.cpu_percent(interval=0.1)
            
            memory_info = psutil.virtual_memory()
            memory_usage = memory_info.percent

            status_text = f"🟢 원격 인프라 가동 정상 (CPU: {cpu_usage}%, MEM: {memory_usage}%)"

            # 2. include_disk 인자가 true일 경우 실제 디스크 공간 계산
            include_disk = args.get("include_disk", False)
            if str(include_disk).lower() == "true":
                # 루트 캐시('/') 경로의 디스크 사용량 조회 (기가바이트 단위 변환)
                disk_info = psutil.disk_usage('/')
                # bytes 단위를 GB 단위로 보기 쉽게 변환 (1024^3)
                free_disk_gb = round(disk_info.free / (1024 ** 3), 1)
                status_text += f" / Disk 공간 잔여: {free_disk_gb}GB"

            return {"content": [{"type": "text", "text": status_text}]}

        elif tool_name == "fetch_remote_data":
            data_id = args.get("data_id")
            if not data_id:
                raise ValueError("필수 파라미터 'data_id' 값이 누락되었습니다.")
            result_payload = f"📑 [데이터 조회 완료] 식별자: {data_id} -> 상태: Active"
            return {"content": [{"type": "text", "text": result_payload}]}

        # ── 🔌 지그비(Zigbee) 도구 실시간 라우팅 비즈니스 로직 ──
        
        elif tool_name == "zigbee_permit_join":
            duration = int(args.get("duration", 60))
            await app.state.zigbee.permit(duration)  
            return {"content": [{"type": "text", "text": f"✅ {duration}초 동안 지그비 페어링(Permit Join)망을 활성화합니다. 기기의 버튼을 누르세요."}]}

        elif tool_name == "zigbee_list_devices":
            raw_devices = app.state.zigbee.devices
            device_list = []
            
            for ieee, dev in raw_devices.items():
                ieee_str = str(ieee)
                cached_data = zigbee_handler.latest_states.get(ieee_str, {}) if zigbee_handler else {}
                device_list.append({
                    "ieee": ieee_str,
                    "nwk": f"0x{dev.nwk:04X}",
                    "manufacturer": dev.manufacturer,
                    "model": dev.model,
                    "cached_states": cached_data
                })
            return {"content": [{"type": "text", "text": f"📡 [연결된 지그비 기기 정보 리스트]\n{device_list}"}]}

        elif tool_name == "zigbee_control_switch":
            ieee_address = args.get("ieee_address")
            target_state = args.get("state")
            
            from zigpy.types import EUI64
            try:
                # 안전한 파싱 핸들링 (버전 호환용 문자열 가공)
                target_ieee = EUI64.deserialize(
                    bytes.fromhex(ieee_address.replace(":", ""))
                )[0]
                device = app.state.zigbee.get_device(target_ieee)
            except (KeyError, ValueError):
                return {"content": [{"type": "text", "text": f"❌ 기기 맵에서 주소 [{ieee_address}]를 가진 장치를 탐색하지 못했습니다."}]}
            
            for endpoint_id, endpoint in device.endpoints.items():
                if endpoint_id == 0:
                    continue  # ZDO 공통 스킵
                if 6 in endpoint.in_clusters:
                    onoff_cluster = endpoint.in_clusters[6]
                    if target_state == "ON":
                        await onoff_cluster.on()
                    else:
                        await onoff_cluster.off()
                    return {"content": [{"type": "text", "text": f"💡 [{ieee_address}] 장치 제어 성공 -> {target_state}"}]}
                    
            return {"content": [{"type": "text", "text": f"❌ 해당 기기({ieee_address})는 On/Off 원격 스위칭(Cluster 6)을 지원하지 않는 스펙입니다."}]}

    except Exception as biz_error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"도구 내부 가동 중 에러 발생: {str(biz_error)}"
        )
