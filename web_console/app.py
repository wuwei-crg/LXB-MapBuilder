"""
LXB Web Console - Flask Backend
鐢ㄤ簬鍙鍖栬皟璇?LXB-Link 鍗忚鐨?Web 鎺у埗鍙?"""

from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
import sys
import os
import base64
import json
import io
import re
import threading
import uuid
import gzip
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

# 灏濊瘯鍔犺浇 python-dotenv (濡傛灉瀛樺湪)
try:
    from dotenv import load_dotenv
    # 鍔犺浇 .env 鏂囦欢
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    load_dotenv(env_path)
except ImportError:
    pass

# 璁剧疆 HF_TOKEN 鐜鍙橀噺 (鐢ㄤ簬 Hugging Face 妯″瀷涓嬭浇)
if os.getenv('HF_TOKEN'):
    os.environ['HF_TOKEN'] = os.getenv('HF_TOKEN')
    print("[app.py] HF_TOKEN is configured")

# 娣诲姞椤圭洰鏍圭洰褰曞埌 Python 璺緞
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.lxb_link.client import LXBLinkClient
from src.lxb_link.constants import (
    KEY_HOME,
    KEY_BACK,
    KEY_ENTER,
    KEY_MENU,
    KEY_RECENT,
)

app = Flask(__name__)
CORS(app)  # 鍏佽璺ㄥ煙璇锋眰

# Global client instance
client = None
connection_info = {
    'connected': False,
    'host': None,
    'port': None,
    'connection_id': None,
    'running_tasks': 0,
    'total_connections': 0,
}

TASKS = {}
TASKS_LOCK = threading.Lock()
LOG_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'tasks'))

CONNECTIONS: Dict[str, "ConnectionRecord"] = {}
CONNECTIONS_LOCK = threading.RLock()
CURRENT_CONNECTION_ID: Optional[str] = None

CORTEX_LLM_CONFIG_FILE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), '.cortex_llm_planner.json')
)


@dataclass
class ConnectionRecord:
    connection_id: str
    host: str
    port: int
    source: str
    client: LXBLinkClient
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "connected"
    running_tasks: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection_public(record: ConnectionRecord) -> dict:
    return {
        'connection_id': record.connection_id,
        'host': record.host,
        'port': record.port,
        'source': record.source,
        'status': record.status,
        'created_at': record.created_at,
        'last_seen': record.last_seen,
        'running_tasks': int(record.running_tasks),
    }


def _sync_connection_info() -> None:
    global client, connection_info
    with CONNECTIONS_LOCK:
        current = CONNECTIONS.get(CURRENT_CONNECTION_ID) if CURRENT_CONNECTION_ID else None
        total_running = sum(int(c.running_tasks) for c in CONNECTIONS.values())
        connection_info = {
            'connected': bool(current and current.status == 'connected'),
            'host': current.host if current else None,
            'port': current.port if current else None,
            'connection_id': current.connection_id if current else None,
            'running_tasks': int(total_running),
            'total_connections': len(CONNECTIONS),
        }
        client = current.client if current else None


def _create_connection(host: str, port: int, source: str = 'manual', set_current: bool = True) -> ConnectionRecord:
    global CURRENT_CONNECTION_ID
    c = LXBLinkClient(host, port, timeout=2.0)
    c.connect()
    c.handshake()
    record = ConnectionRecord(
        connection_id=str(uuid.uuid4()),
        host=str(host),
        port=int(port),
        source=source,
        client=c,
    )
    with CONNECTIONS_LOCK:
        CONNECTIONS[record.connection_id] = record
        if set_current:
            CURRENT_CONNECTION_ID = record.connection_id
    _sync_connection_info()
    return record


def _find_connection_by_host_port(host: str, port: int) -> Optional[ConnectionRecord]:
    with CONNECTIONS_LOCK:
        for rec in CONNECTIONS.values():
            if rec.host == str(host) and int(rec.port) == int(port) and rec.status == 'connected':
                return rec
    return None


def _select_connection(connection_id: str) -> ConnectionRecord:
    global CURRENT_CONNECTION_ID
    with CONNECTIONS_LOCK:
        rec = CONNECTIONS.get(connection_id)
        if not rec:
            raise RuntimeError('connection_not_found')
        if rec.status != 'connected':
            raise RuntimeError('connection_not_connected')
        CURRENT_CONNECTION_ID = connection_id
        rec.last_seen = _now_iso()
    _sync_connection_info()
    return rec


def _get_connection(connection_id: Optional[str] = None, require: bool = True) -> Optional[ConnectionRecord]:
    with CONNECTIONS_LOCK:
        cid = connection_id or CURRENT_CONNECTION_ID
        rec = CONNECTIONS.get(cid) if cid else None
        if rec and rec.status == 'connected':
            rec.last_seen = _now_iso()
            return rec
    if require:
        raise RuntimeError('device not connected')
    return None


def _require_client_response():
    """Return a Flask error response when no active client is available."""
    _sync_connection_info()
    if client:
        return None
    return jsonify({'success': False, 'message': 'device not connected'}), 400


def _disconnect_connection(connection_id: Optional[str] = None) -> None:
    global CURRENT_CONNECTION_ID
    with CONNECTIONS_LOCK:
        cid = connection_id or CURRENT_CONNECTION_ID
        rec = CONNECTIONS.get(cid) if cid else None
        if not rec:
            return
        try:
            rec.client.disconnect()
        except Exception:
            pass
        rec.status = 'disconnected'
        rec.last_seen = _now_iso()
        if cid in CONNECTIONS:
            del CONNECTIONS[cid]
        if CURRENT_CONNECTION_ID == cid:
            CURRENT_CONNECTION_ID = next(iter(CONNECTIONS.keys()), None)
    _sync_connection_info()


def _default_cortex_llm_config() -> dict:
    return {
        'api_base_url': os.getenv('CORTEX_LLM_API_BASE_URL', ''),
        'api_key': os.getenv('CORTEX_LLM_API_KEY', ''),
        'model_name': os.getenv('CORTEX_LLM_MODEL_NAME', 'qwen-plus'),
        'temperature': float(os.getenv('CORTEX_LLM_TEMPERATURE', '0.1')),
        'timeout': int(os.getenv('CORTEX_LLM_TIMEOUT', '30')),
        'vision_jpeg_quality': int(os.getenv('CORTEX_VISION_JPEG_QUALITY', '35')),
        'node_exists_retries': int(os.getenv('CORTEX_NODE_EXISTS_RETRIES', '3')),
        'node_exists_interval_sec': float(os.getenv('CORTEX_NODE_EXISTS_INTERVAL_SEC', '0.6')),
        'touch_mode': os.getenv('CORTEX_TOUCH_MODE', 'shell_first'),
        # Route/FSM runtime defaults (persisted in same config file).
        'map_filepath': '',
        'package_name': '',
        'reconnect_before_run': True,
        'use_llm_planner': True,
        'route_recovery_enabled': False,
        'max_route_restarts': 0,
        'use_vlm_takeover': False,
        'fsm_max_turns': 40,
        'fsm_max_commands_per_turn': 1,
        'fsm_max_vision_turns': 20,
        'fsm_action_interval_sec': 0.8,
        'fsm_screenshot_settle_sec': 0.6,
        'fsm_tap_bind_clickable': False,
        'fsm_tap_jitter_sigma_px': 2.0,
        'fsm_swipe_jitter_sigma_px': 4.0,
        'fsm_swipe_duration_jitter_ratio': 0.12,
        'fsm_xml_stable_interval_sec': 0.3,
        'fsm_xml_stable_samples': 4,
        'fsm_xml_stable_timeout_sec': 4.0,
    }


def _load_cortex_llm_config() -> dict:
    cfg = _default_cortex_llm_config()
    if not os.path.exists(CORTEX_LLM_CONFIG_FILE):
        return cfg
    try:
        with open(CORTEX_LLM_CONFIG_FILE, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update(stored)
    except Exception:
        pass
    return cfg


def _save_cortex_llm_config(config: dict) -> None:
    current = _default_cortex_llm_config()
    current.update(config or {})
    os.makedirs(os.path.dirname(CORTEX_LLM_CONFIG_FILE), exist_ok=True)
    with open(CORTEX_LLM_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)


def _task_create(task_type: str, connection_id: str = '', user_task: str = '') -> str:
    task_id = str(uuid.uuid4())
    with TASKS_LOCK:
        TASKS[task_id] = {
            'task_id': task_id,
            'type': task_type,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'started_at': None,
            'ended_at': None,
            'status': 'created',
            'connection_id': connection_id,
            'user_task': user_task,
            'events': [],
            'done': False,
            'success': False,
            'result': None,
            'message': '',
            'cancel_requested': False,
            'log_file': '',
            'summary_file': '',
        }
    return task_id


def _task_append(task_id: str, event: dict) -> None:
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if t:
            if not t.get('started_at'):
                t['started_at'] = _now_iso()
                t['status'] = 'running'
            t['events'].append(event)


def _task_finish(task_id: str, success: bool, result: dict = None, message: str = '') -> None:
    snapshot = None
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return
        t['done'] = True
        t['success'] = bool(success)
        t['result'] = result or {}
        t['message'] = message or ''
        t['ended_at'] = _now_iso()
        t['status'] = 'success' if success else ('cancelled' if message == 'task_cancelled' else 'failed')
        snapshot = dict(t)
    if snapshot:
        _task_persist(snapshot)


def _task_persist(task_snapshot: dict) -> None:
    try:
        created = task_snapshot.get('created_at') or _now_iso()
        day = created.split('T', 1)[0] or 'unknown'
        day_dir = os.path.join(LOG_ROOT, day)
        os.makedirs(day_dir, exist_ok=True)
        task_id = task_snapshot.get('task_id') or str(uuid.uuid4())
        log_file = os.path.join(day_dir, f'{task_id}.jsonl')
        summary_file = os.path.join(day_dir, f'{task_id}.summary.json')
        with open(log_file, 'w', encoding='utf-8') as f:
            for e in task_snapshot.get('events') or []:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
        summary_payload = {
            'task_id': task_id,
            'type': task_snapshot.get('type'),
            'connection_id': task_snapshot.get('connection_id'),
            'status': task_snapshot.get('status'),
            'success': bool(task_snapshot.get('success')),
            'message': task_snapshot.get('message') or '',
            'user_task': task_snapshot.get('user_task') or '',
            'created_at': task_snapshot.get('created_at'),
            'started_at': task_snapshot.get('started_at'),
            'ended_at': task_snapshot.get('ended_at'),
            'event_count': len(task_snapshot.get('events') or []),
            'result': task_snapshot.get('result') or {},
            'log_file': log_file,
        }
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary_payload, f, ensure_ascii=False, indent=2)
        with TASKS_LOCK:
            t = TASKS.get(task_id)
            if t:
                t['log_file'] = log_file
                t['summary_file'] = summary_file
    except Exception:
        pass


def _task_is_cancel_requested(task_id: str) -> bool:
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return False
        return bool(t.get('cancel_requested'))


def _ensure_connected(host: str, port: int) -> None:
    """Ensure at least one selected active connection."""
    rec = _find_connection_by_host_port(host, int(port))
    if rec:
        _select_connection(rec.connection_id)
        return
    _create_connection(host, int(port), source='mobile_auto', set_current=False)


def _prepare_link_for_task(run_client, reconnect: bool = True) -> None:
    """
    Recover link state after abrupt task interruption.
    """
    if not run_client:
        raise RuntimeError('device not connected')
    if reconnect:
        run_client.reconnect(handshake=True)
    else:
        try:
            run_client.reset_runtime_state(reset_seq=False)
        except Exception:
            pass


def _resolve_run_connection(data: dict, allow_mobile_auto: bool = False) -> ConnectionRecord:
    connection_id = str(data.get('connection_id') or '').strip()
    if connection_id:
        return _get_connection(connection_id=connection_id, require=True)

    if allow_mobile_auto:
        lxb_port = data.get('lxb_port')
        if lxb_port:
            host = request.remote_addr or ''
            rec = _find_connection_by_host_port(host, int(lxb_port))
            if rec:
                return rec
            return _create_connection(host, int(lxb_port), source='mobile_auto', set_current=False)

    return _get_connection(connection_id=None, require=True)


def _build_llm_complete(config: dict):
    from openai import OpenAI

    api_base_url = (config.get('api_base_url') or '').strip()
    api_key = (config.get('api_key') or '').strip()
    model_name = (config.get('model_name') or '').strip()
    if not api_base_url or not api_key or not model_name:
        raise ValueError('LLM 閰嶇疆涓嶅畬鏁达細api_base_url / api_key / model_name 蹇呭～')

    client = OpenAI(
        base_url=api_base_url,
        api_key=api_key,
        timeout=float(config.get('timeout', 30)),
    )
    temperature = float(config.get('temperature', 0.1))

    def complete(prompt: str) -> str:
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a route planner. Output strict JSON only with keys: "
                        "package_name, target_page."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        return (response.choices[0].message.content or '').strip()

    return complete


def _build_llm_complete_fsm(config: dict):
    from openai import OpenAI

    api_base_url = (config.get('api_base_url') or '').strip()
    api_key = (config.get('api_key') or '').strip()
    model_name = (config.get('model_name') or '').strip()
    if not api_base_url or not api_key or not model_name:
        raise ValueError('LLM 閰嶇疆涓嶅畬鏁达細api_base_url / api_key / model_name 蹇呭～')

    client = OpenAI(
        base_url=api_base_url,
        api_key=api_key,
        timeout=float(config.get('timeout', 30)),
    )
    temperature = float(config.get('temperature', 0.1))

    def _detect_state(prompt: str) -> str:
        text = (prompt or '')
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('State='):
                return line.split('=', 1)[1].strip().upper()
        return ""

    def _state_system_prompt(state: str) -> str:
        base = (
            "You are a finite-state mobile planner.\n"
            "Follow the state-specific output format strictly.\n"
            "Output must contain exactly one <command>...</command>.\n"
            "Do not output markdown.\n"
        )
        if state == "APP_RESOLVE":
            return base + (
                "Current state: APP_RESOLVE.\n"
                "Analyze app candidates first, then output one command.\n"
                "Use <app_analysis>...</app_analysis> plus <command>...\n"
                "Inside <app_analysis>, include a short <reflection> lesson.\n"
            )
        if state == "ROUTE_PLAN":
            return base + (
                "Current state: ROUTE_PLAN.\n"
                "Analyze target page candidates first, then output one command.\n"
                "Use <route_plan_analysis>...</route_plan_analysis> plus <command>...\n"
                "Inside <route_plan_analysis>, include a short <reflection> lesson.\n"
            )
        if state == "VISION_ACT":
            return base + (
                "Current state: VISION_ACT.\n"
                "Analyze current page first, then reason next step, then output one command.\n"
                "Use <vision_analysis>...</vision_analysis> plus <command>...\n"
                "Inside <vision_analysis>, include <step_review> for recent multi-step outcomes, and <reflection> as cumulative lesson from recent 3~5 steps with action intent to avoid next.\n"
                "One turn = one command.\n"
            )
        return base + "Current state unknown. Follow user prompt format exactly.\n"

    def complete(prompt: str) -> str:
        state = _detect_state(prompt)
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[
                {
                    "role": "system",
                    "content": _state_system_prompt(state),
                },
                {"role": "user", "content": prompt},
            ],
        )
        return (response.choices[0].message.content or '').strip()

    return complete


def _build_llm_complete_with_image(config: dict):
    from openai import OpenAI

    api_base_url = (config.get('api_base_url') or '').strip()
    api_key = (config.get('api_key') or '').strip()
    model_name = (config.get('model_name') or '').strip()
    if not api_base_url or not api_key or not model_name:
        raise ValueError('LLM 閰嶇疆涓嶅畬鏁达細api_base_url / api_key / model_name 蹇呭～')

    client = OpenAI(
        base_url=api_base_url,
        api_key=api_key,
        timeout=float(config.get('timeout', 30)),
    )
    temperature = float(config.get('temperature', 0.1))
    jpeg_quality = int(config.get('vision_jpeg_quality', 35))

    def _detect_state(prompt: str) -> str:
        text = (prompt or '')
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('State='):
                return line.split('=', 1)[1].strip().upper()
        return ""

    def _state_system_prompt(state: str) -> str:
        base = (
            "You are a mobile VLM planner.\n"
            "Follow the state-specific output format strictly.\n"
            "Output must contain exactly one <command>...</command>.\n"
            "Do not output markdown.\n"
        )
        if state == "VISION_ACT":
            return base + (
                "Current state: VISION_ACT.\n"
                "First describe page_state, then next_step_reasoning, then one command.\n"
                "Use <vision_analysis>...</vision_analysis> plus <command>...\n"
                "Inside <vision_analysis>, include <step_review> for recent multi-step outcomes, and <reflection> as cumulative lesson from recent 3~5 steps with action intent to avoid next.\n"
                "One turn = one command.\n"
            )
        return base + "Follow user prompt format exactly.\n"

    def _reencode_jpeg(image_bytes: bytes, quality: int) -> bytes:
        quality = max(10, min(95, int(quality)))
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image_bytes)) as img:
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                out = io.BytesIO()
                img.save(out, format='JPEG', quality=quality, optimize=True)
                return out.getvalue()
        except Exception:
            return image_bytes

    def complete_with_image(prompt: str, image_bytes: bytes) -> str:
        state = _detect_state(prompt)
        compressed = _reencode_jpeg(image_bytes, jpeg_quality)
        image_b64 = base64.b64encode(compressed).decode('ascii')
        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[
                {
                    "role": "system",
                    "content": _state_system_prompt(state),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ],
                },
            ],
        )
        return (response.choices[0].message.content or '').strip()

    return complete_with_image


def _build_llm_task_summary(config: dict):
    from openai import OpenAI

    api_base_url = (config.get('api_base_url') or '').strip()
    api_key = (config.get('api_key') or '').strip()
    model_name = (config.get('model_name') or '').strip()
    if not api_base_url or not api_key or not model_name:
        raise ValueError('LLM config missing: api_base_url / api_key / model_name')

    client = OpenAI(
        base_url=api_base_url,
        api_key=api_key,
        timeout=float(config.get('timeout', 30)),
    )

    def summarize(user_task: str, run_result: dict) -> str:
        llm_hist = (run_result.get('llm_history') or [])[-12:]
        route_trace = (run_result.get('route_trace') or [])[-12:]
        status = str(run_result.get('status') or '')
        state = str(run_result.get('state') or '')
        reason = str(run_result.get('reason') or '')
        prompt = "\n".join([
            "Generate a concise task summary from the execution signals below.",
            "Requirements:",
            "1) Output summary body only (no title).",
            "2) If successful, focus on user-visible outcome.",
            "3) If failed, state phase and likely reason.",
            "4) Keep it concise.",
            f"User task: {user_task}",
            f"Task status: {status}",
            f"Final FSM state: {state}",
            f"Failure reason: {reason}",
            f"Route trace (JSON): {json.dumps(route_trace, ensure_ascii=False)}",
            f"Execution history (JSON): {json.dumps(llm_hist, ensure_ascii=False)}",
        ])
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": "You are a mobile automation summarizer. Use only provided facts and do not speculate.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        return (response.choices[0].message.content or '').strip()

    return summarize


def _fallback_task_summary(user_task: str, run_result: dict) -> str:
    status = str(run_result.get('status') or '')
    state = str(run_result.get('state') or '')
    reason = str(run_result.get('reason') or '')
    llm_hist = run_result.get('llm_history') or []

    if status == 'success':
        observations = []
        for item in reversed(llm_hist):
            s = item.get('structured') or {}
            ps = str(s.get('page_state') or '').strip()
            if ps:
                observations.append(ps)
            if len(observations) >= 2:
                break
        if observations:
            obs_text = "; ".join(reversed(observations))
            return f'Task completed for "{user_task}". Final observations: {obs_text}.'
        return f'Task completed for "{user_task}".'
    return f'Task not completed. Ended at state={state}, reason={reason or "unknown"}.'


def _extract_json_object(text: str) -> dict:
    text = (text or '').strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _infer_app_name_from_package(package_name: str) -> str:
    if not package_name:
        return ''
    parts = [x for x in package_name.split('.') if x]
    if not parts:
        return package_name
    tail = parts[-1]
    return tail.replace('_', ' ')


def _normalize_installed_apps(raw_apps) -> list:
    out = []
    for item in raw_apps or []:
        if isinstance(item, dict):
            package_name = str(item.get('package') or '').strip()
            if not package_name:
                continue
            label = str(item.get('name') or item.get('label') or '').strip()
            inferred = _infer_app_name_from_package(package_name)
            out.append({
                'package': package_name,
                'name': label or inferred,
                'label': label,
                'name_source': 'label' if label else 'inferred',
            })
            continue

        package_name = str(item or '').strip()
        if not package_name:
            continue
        inferred = _infer_app_name_from_package(package_name)
        out.append({
            'package': package_name,
            'name': inferred,
            'label': '',
            'name_source': 'inferred',
        })
    return out


def _has_map_for_package(base_dir: str, package_name: str) -> bool:
    pkg_path = os.path.join(base_dir, package_name.replace('.', '_'))
    if not os.path.isdir(pkg_path):
        return False
    import glob
    return len(glob.glob(os.path.join(pkg_path, 'nav_map_*.json'))) > 0


def _select_package_by_llm(
    llm_complete,
    user_task: str,
    app_candidates: list,
) -> dict:
    rows = []
    for app in app_candidates[:120]:
        rows.append({
            'package': app.get('package', ''),
            'name': app.get('name', ''),
        })

    prompt = (
        "You are selecting an Android app package for user intent routing.\n"
        "Output JSON only:\n"
        '{"package_name":"...","reason":"..."}\n'
        "Rules:\n"
        "1) package_name must be chosen from candidate package list.\n"
        "2) prefer semantic app name match first, then package token match.\n\n"
        f"user_task:\n{user_task}\n\n"
        f"candidates:\n{json.dumps(rows, ensure_ascii=False)}"
    )
    raw = llm_complete(prompt)
    payload = _extract_json_object(raw)
    package_name = str(payload.get('package_name') or '').strip()
    reason = str(payload.get('reason') or '').strip()
    return {'package_name': package_name, 'reason': reason, 'raw': raw}


def _select_target_page_by_llm(
    llm_complete,
    user_task: str,
    map_path: str,
) -> dict:
    with open(map_path, 'r', encoding='utf-8') as f:
        raw_map = json.load(f)

    pages = raw_map.get('pages') or {}
    transitions = raw_map.get('transitions') or []

    page_rows = []
    for page_id, page in pages.items():
        page_rows.append({
            'page_id': page_id,
            'legacy_page_id': page.get('legacy_page_id', ''),
            'name': page.get('name', ''),
            'description': page.get('description', ''),
            'features': (page.get('features') or [])[:12],
            'aliases': (page.get('target_aliases') or [])[:8],
        })

    edge_rows = []
    for t in transitions:
        edge_rows.append({
            'from': t.get('from', ''),
            'to': t.get('to', ''),
            'trigger': t.get('description', ''),
        })

    prompt = (
        "You are selecting target_page for mobile app routing.\n"
        "Output JSON only:\n"
        '{"target_page":"...","reason":"..."}\n'
        "Rules:\n"
        "1) target_page must be one page_id or legacy_page_id from pages.\n"
        "2) You MUST use semantic fields: name, description, features, aliases.\n"
        "3) Prefer the page whose semantics most directly satisfy the user task.\n\n"
        f"user_task:\n{user_task}\n\n"
        f"map:\n{json.dumps({'package': raw_map.get('package', ''), 'pages': page_rows, 'transitions': edge_rows}, ensure_ascii=False)}"
    )
    raw = llm_complete(prompt)
    payload = _extract_json_object(raw)
    target_page = str(payload.get('target_page') or '').strip()
    reason = str(payload.get('reason') or '').strip()
    return {'target_page': target_page, 'reason': reason, 'raw': raw}


def _build_page_candidates_from_map(map_path: str) -> list:
    try:
        with open(map_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return []

    rows = []
    pages = raw.get('pages', {}) if isinstance(raw, dict) else {}
    for page_id, page in pages.items():
        if not isinstance(page, dict):
            continue
        rows.append({
            'page_id': str(page_id),
            'legacy_page_id': str(page.get('legacy_page_id') or ''),
            'name': str(page.get('name') or ''),
            'description': str(page.get('description') or ''),
            'features': list(page.get('features') or [])[:8],
            'aliases': list(page.get('target_aliases') or [])[:6],
        })
    return rows


class _FixedPlanPlanner:
    def __init__(self, package_name: str, target_page: str):
        self.package_name = package_name
        self.target_page = target_page

    def plan(self, user_task, route_map):
        from src.cortex import RoutePlan
        pkg = self.package_name or route_map.package
        return RoutePlan(pkg, self.target_page)


class _FSMPlannerBridge:
    """
    Bridge planner for FSM mode.
    - APP_RESOLVE is pinned to selected package when available.
    - Other states delegate to FSM LLM planner when provided.
    - Without LLM planner, VISION_ACT defaults to DONE for route-only debugging.
    """

    def __init__(self, selected_package: str = "", llm_planner=None):
        self.selected_package = (selected_package or "").strip()
        self.llm_planner = llm_planner

    def plan(self, state, prompt, context):
        state_name = getattr(state, "value", str(state))
        if state_name == "APP_RESOLVE" and self.selected_package:
            return f"SET_APP {self.selected_package}"

        if self.llm_planner is not None:
            return self.llm_planner.plan(state, prompt, context)

        if state_name == "VISION_ACT":
            return "DONE"
        return "FAIL llm_planner_disabled"

    def plan_vision(self, state, prompt, context, screenshot):
        if self.llm_planner is not None and hasattr(self.llm_planner, 'plan_vision'):
            return self.llm_planner.plan_vision(state, prompt, context, screenshot)
        return self.plan(state, prompt, context)


@app.route('/')
def index():
    """Main dashboard page."""
    return render_template('index.html')


@app.route('/command_studio')
def command_studio():
    """鎸囦护璋冭瘯椤甸潰"""
    return render_template('command_studio.html')


@app.route('/map_builder')
def map_builder():
    """Map Builder 椤甸潰"""
    return render_template('map_builder.html')


@app.route('/map_viewer')
def map_viewer():
    """Map Viewer 椤甸潰"""
    return render_template('map_viewer.html')


@app.route('/map_publish')
def map_publish():
    """Map Publish page."""
    return render_template('map_publish.html')


@app.route('/api/connect', methods=['POST'])
def connect():
    """Legacy connect endpoint: create/select active connection."""
    data = request.json or {}
    host = (data.get('host') or '192.168.1.100').strip()
    port = int(data.get('port') or 12345)
    try:
        existed = _find_connection_by_host_port(host, port)
        if existed:
            _select_connection(existed.connection_id)
            return jsonify({'success': True, 'message': f'宸插垏鎹㈠埌 {host}:{port}', 'connection': _connection_public(existed)})
        rec = _create_connection(host, port, source='manual', set_current=True)
        return jsonify({'success': True, 'message': f'鎴愬姛杩炴帴鍒?{host}:{port}', 'connection': _connection_public(rec)})
    except Exception as e:
        return jsonify({'success': False, 'message': f'杩炴帴澶辫触: {str(e)}'}), 500


@app.route('/api/disconnect', methods=['POST'])
def disconnect():
    """Legacy disconnect endpoint: disconnect current active connection."""
    try:
        _disconnect_connection(None)
        return jsonify({'success': True, 'message': '宸叉柇寮€杩炴帴'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'鏂紑澶辫触: {str(e)}'}), 500


@app.route('/api/status', methods=['GET'])
def status():
    """Get current connection and pool status."""
    _sync_connection_info()
    with CONNECTIONS_LOCK:
        connections = [_connection_public(x) for x in CONNECTIONS.values()]
    payload = dict(connection_info)
    payload['connections'] = connections
    return jsonify(payload)


@app.route('/api/connections/create', methods=['POST'])
def connections_create():
    data = request.json or {}
    host = (data.get('host') or '192.168.1.100').strip()
    port = int(data.get('port') or 12345)
    source = (data.get('source') or 'manual').strip() or 'manual'
    set_current = bool(data.get('set_current', True))
    try:
        existed = _find_connection_by_host_port(host, port)
        if existed:
            if set_current:
                _select_connection(existed.connection_id)
            return jsonify({'success': True, 'connection': _connection_public(existed), 'reused': True})
        rec = _create_connection(host, port, source=source, set_current=set_current)
        return jsonify({'success': True, 'connection': _connection_public(rec)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/connections/list', methods=['GET'])
def connections_list():
    _sync_connection_info()
    with CONNECTIONS_LOCK:
        rows = [_connection_public(x) for x in CONNECTIONS.values()]
    return jsonify({'success': True, 'current_connection_id': connection_info.get('connection_id'), 'data': rows})


@app.route('/api/connections/select', methods=['POST'])
def connections_select():
    data = request.json or {}
    connection_id = (data.get('connection_id') or '').strip()
    if not connection_id:
        return jsonify({'success': False, 'message': 'connection_id is required'}), 400
    try:
        rec = _select_connection(connection_id)
        return jsonify({'success': True, 'connection': _connection_public(rec)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400


@app.route('/api/connections/<connection_id>/disconnect', methods=['POST'])
def connections_disconnect(connection_id):
    try:
        _disconnect_connection(connection_id)
        return jsonify({'success': True, 'connection_id': connection_id})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Link Layer (0x00-0x0F)
# =============================================================================

@app.route('/api/command/handshake', methods=['POST'])
def cmd_handshake():
    """Send handshake command."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        response = client.handshake()
        return jsonify({
            'success': True,
            'message': '鎻℃墜鎴愬姛',
            'response': {
                'length': len(response)
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/heartbeat', methods=['POST'])
def cmd_heartbeat():
    """鍙戦€?HEARTBEAT 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        response = client.heartbeat()
        return jsonify({
            'success': True,
            'message': '蹇冭烦鎴愬姛',
            'response': {
                'length': len(response),
                'data': list(response) if response else []
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Input Layer (0x10-0x1F)
# =============================================================================

@app.route('/api/command/tap', methods=['POST'])
def cmd_tap():
    """鍙戦€?TAP 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    x = data.get('x', 500)
    y = data.get('y', 800)

    try:
        response = client.tap(x, y)
        return jsonify({
            'success': True,
            'message': f'TAP ({x}, {y}) 鎴愬姛',
            'response': {
                'length': len(response),
                'data': list(response) if response else []
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/swipe', methods=['POST'])
def cmd_swipe():
    """鍙戦€?SWIPE 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    x1 = data.get('x1', 500)
    y1 = data.get('y1', 1000)
    x2 = data.get('x2', 500)
    y2 = data.get('y2', 500)
    duration = data.get('duration', 300)

    try:
        response = client.swipe(x1, y1, x2, y2, duration)
        return jsonify({
            'success': True,
            'message': f'SWIPE ({x1},{y1})鈫?{x2},{y2}) 鎴愬姛',
            'response': {
                'length': len(response),
                'data': list(response) if response else []
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/long_press', methods=['POST'])
def cmd_long_press():
    """鍙戦€?LONG_PRESS 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    x = data.get('x', 500)
    y = data.get('y', 800)
    duration = data.get('duration', 1000)

    try:
        response = client.long_press(x, y, duration)
        return jsonify({
            'success': True,
            'message': f'LONG_PRESS ({x}, {y}) {duration}ms 鎴愬姛',
            'response': {
                'length': len(response),
                'data': list(response) if response else []
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/unlock', methods=['POST'])
def cmd_unlock():
    """鍙戦€?UNLOCK 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        success = client.unlock()
        return jsonify({
            'success': success,
            'message': '瑙ｉ攣鎴愬姛' if success else '瑙ｉ攣澶辫触'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Input Extension (0x20-0x2F)
# =============================================================================

@app.route('/api/command/input_text', methods=['POST'])
def cmd_input_text():
    """鍙戦€?INPUT_TEXT 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    text = data.get('text', 'Hello LXB')
    clear_first = data.get('clear_first', False)
    press_enter = data.get('press_enter', False)

    try:
        status, actual_method = client.input_text(
            text,
            clear_first=clear_first,
            press_enter=press_enter
        )
        return jsonify({
            'success': status == 1,
            'message': f'杈撳叆鏂囨湰 "{text}" 鎴愬姛' if status == 1 else '杈撳叆鏂囨湰澶辫触',
            'response': {
                'status': status,
                'method': actual_method
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/key_event', methods=['POST'])
def cmd_key_event():
    """鍙戦€?KEY_EVENT 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    keycode = data.get('keycode', KEY_BACK)
    action = data.get('action', 2)  # 2 = CLICK

    # 鏀寔鎸夊悕绉版寚瀹氭寜閿?
    key_map = {
        'home': KEY_HOME,
        'back': KEY_BACK,
        'enter': KEY_ENTER,
        'menu': KEY_MENU,
        'recent': KEY_RECENT
    }
    if isinstance(keycode, str):
        keycode = key_map.get(keycode.lower(), KEY_BACK)

    try:
        response = client.key_event(keycode, action)
        return jsonify({
            'success': True,
            'message': f'KEY_EVENT keycode={keycode} 鎴愬姛',
            'response': {
                'length': len(response) if response else 0
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Sense Layer (0x30-0x3F)
# =============================================================================

@app.route('/api/command/get_activity', methods=['POST'])
def cmd_get_activity():
    """鍙戦€?GET_ACTIVITY 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        success, package_name, activity_name = client.get_activity()
        return jsonify({
            'success': success,
            'message': '鑾峰彇 Activity 鎴愬姛' if success else '鑾峰彇 Activity 澶辫触',
            'response': {
                'package': package_name,
                'activity': activity_name
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/get_screen_state', methods=['POST'])
def cmd_get_screen_state():
    """Send GET_SCREEN_STATE command."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        success, state = client.get_screen_state()
        state_names = {0: 'screen_off', 1: 'screen_on_unlocked', 2: 'screen_on_locked'}
        return jsonify({
            'success': success,
            'message': f'screen state: {state_names.get(state, "unknown")}',
            'response': {
                'state': state,
                'state_name': state_names.get(state, 'unknown')
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/get_screen_size', methods=['POST'])
def cmd_get_screen_size():
    """鍙戦€?GET_SCREEN_SIZE 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        success, width, height, density = client.get_screen_size()
        return jsonify({
            'success': success,
            'message': f'灞忓箷灏哄: {width}x{height} @{density}dpi',
            'response': {
                'width': width,
                'height': height,
                'density': density
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/touch_mode', methods=['POST'])
def cmd_touch_mode():
    """Set touch execution mode on Android side."""
    error_response = _require_client_response()
    if error_response:
        return error_response
    try:
        data = request.json or {}
        mode = str(data.get('mode') or 'shell_first').strip()
        shell_first = mode != 'uiautomation_first'
        ok = client.set_touch_mode(shell_first=shell_first)
        return jsonify({
            'success': bool(ok),
            'message': f"touch_mode set to {'shell_first' if shell_first else 'uiautomation_first'}",
            'response': {'touch_mode': 'shell_first' if shell_first else 'uiautomation_first'}
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/find_node', methods=['POST'])
def cmd_find_node():
    """鍙戦€?FIND_NODE 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    query = data.get('query', '')
    match_type = data.get('match_type', 1)  # MATCH_CONTAINS_TEXT
    multi_match = data.get('multi_match', False)

    try:
        status, results = client.find_node(
            query,
            match_type=match_type,
            multi_match=multi_match
        )
        return jsonify({
            'success': status == 1,
            'message': f'found {len(results)} nodes' if status == 1 else 'node not found',
            'response': {
                'status': status,
                'count': len(results),
                'results': results
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/dump_hierarchy', methods=['POST'])
def cmd_dump_hierarchy():
    """鍙戦€?DUMP_HIERARCHY 鍛戒护锛岃幏鍙栧畬鏁?UI 灞傜骇缁撴瀯"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    max_depth = data.get('max_depth', 0)  # 0 = 鏃犻檺鍒?
    try:
        hierarchy = client.dump_hierarchy(max_depth=max_depth)
        node_count = hierarchy.get('node_count', 0)
        nodes = hierarchy.get('nodes', [])

        # Count interactive nodes
        clickable_count = sum(1 for n in nodes if n.get('clickable', False))
        editable_count = sum(1 for n in nodes if n.get('editable', False))
        scrollable_count = sum(1 for n in nodes if n.get('scrollable', False))

        return jsonify({
            'success': True,
            'message': f'UI tree fetched: {node_count} nodes',
            'response': {
                'version': hierarchy.get('version', 1),
                'node_count': node_count,
                'clickable_count': clickable_count,
                'editable_count': editable_count,
                'scrollable_count': scrollable_count,
                'nodes': nodes
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/dump_actions', methods=['POST'])
def cmd_dump_actions():
    """鍙戦€?DUMP_ACTIONS 鍛戒护锛岃幏鍙栧彲浜や簰鑺傜偣 (鐢ㄤ簬璺緞瑙勫垝)"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        actions = client.dump_actions()
        node_count = actions.get('node_count', 0)
        nodes = actions.get('nodes', [])

        # Count node categories
        clickable_count = sum(1 for n in nodes if n.get('clickable', False))
        editable_count = sum(1 for n in nodes if n.get('editable', False))
        scrollable_count = sum(1 for n in nodes if n.get('scrollable', False))
        text_only_count = sum(1 for n in nodes if n.get('text_only', False))

        return jsonify({
            'success': True,
            'message': f'actionable nodes fetched: {node_count}',
            'response': {
                'version': actions.get('version', 1),
                'node_count': node_count,
                'clickable_count': clickable_count,
                'editable_count': editable_count,
                'scrollable_count': scrollable_count,
                'text_only_count': text_only_count,
                'nodes': nodes
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Cortex / Map Debug Layer (0x70-0x7F)
# =============================================================================

def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _pkg_dir_name(package_name: str) -> str:
    return str(package_name or '').strip().replace('.', '_')


def _is_safe_map_path(abs_path: str) -> bool:
    roots = [
        os.path.abspath(os.path.join(_project_root(), 'maps')),
        os.path.abspath(os.path.join(_project_root(), 'sample_maps')),
        os.path.abspath(os.path.join(_project_root(), 'map_repo')),
    ]
    for root in roots:
        if abs_path == root or abs_path.startswith(root + os.sep):
            return True
    return False


def _find_latest_map_for_package(package_name: str) -> Optional[str]:
    import glob

    pkg_dir = _pkg_dir_name(package_name)
    pkg_raw = str(package_name or '').strip()
    candidates = []
    search_roots = [
        os.path.join(_project_root(), 'maps'),
        os.path.join(_project_root(), 'sample_maps'),
    ]
    for root in search_roots:
        path = os.path.join(root, pkg_dir, 'nav_map_*.json')
        for fp in glob.glob(path):
            try:
                candidates.append((os.path.getmtime(fp), fp))
            except Exception:
                pass
    # New local map_repo layout:
    #   map_repo/maps/<package>/<map_id>/nav_map.json.gz
    if pkg_raw:
        map_repo_root = os.path.join(_project_root(), 'map_repo', 'maps')
        patterns = [
            os.path.join(map_repo_root, pkg_raw, '*', 'nav_map.json.gz'),
            os.path.join(map_repo_root, pkg_raw, '*', 'nav_map.json'),
            os.path.join(map_repo_root, pkg_dir, '*', 'nav_map.json.gz'),
            os.path.join(map_repo_root, pkg_dir, '*', 'nav_map.json'),
        ]
        for pat in patterns:
            for fp in glob.glob(pat):
                try:
                    candidates.append((os.path.getmtime(fp), fp))
                except Exception:
                    pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _load_map_json_text(package_name: str, data: dict) -> str:
    """Load map json text from request body or known local map paths."""
    raw_text = (data.get('map_json') or '').strip()
    if raw_text:
        # Validate user-provided text is JSON
        json.loads(raw_text)
        return raw_text

    map_filepath = (data.get('map_filepath') or '').strip()
    if map_filepath:
        abs_filepath = os.path.abspath(map_filepath)
        if not _is_safe_map_path(abs_filepath):
            raise RuntimeError('illegal map_filepath: only maps/, sample_maps/, map_repo/ are allowed')
        if not os.path.exists(abs_filepath):
            raise RuntimeError(f'map file not found: {abs_filepath}')
        obj = _load_json_or_gz(abs_filepath)
        return json.dumps(obj, ensure_ascii=False)

    latest = _find_latest_map_for_package(package_name)
    if latest:
        obj = _load_json_or_gz(latest)
        return json.dumps(obj, ensure_ascii=False)

    raise RuntimeError(f'no usable map found for package={package_name}, provide map_json or map_filepath')


@app.route('/api/command/map_get_info', methods=['POST'])
def cmd_map_get_info():
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    package_name = (data.get('package') or '').strip()
    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    try:
        info = client.map_get_info(package_name)
        ok = bool(info.get('ok'))
        return jsonify({
            'success': ok,
            'message': f'MAP_GET_INFO {"鎴愬姛" if ok else "澶辫触"}: {package_name}',
            'response': info
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/map_set_gz', methods=['POST'])
def cmd_map_set_gz():
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    package_name = (data.get('package') or '').strip()
    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    try:
        map_json_text = _load_map_json_text(package_name, data)
        result = client.map_set_gz(package_name, map_json_text)
        ok = bool(result.get('ok'))
        return jsonify({
            'success': ok,
            'message': f'MAP_SET_GZ {"鎴愬姛" if ok else "澶辫触"}: {package_name}',
            'response': result
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def cmd_cortex_resolve_locator():
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    locator = data.get('locator') or {}
    if not isinstance(locator, dict) or not locator:
        return jsonify({'success': False, 'message': 'locator is required'}), 400

    try:
        result = client.cortex_resolve_locator(locator)
        ok = bool(result.get('ok'))
        msg = 'CORTEX_RESOLVE_LOCATOR 鎴愬姛' if ok else f'CORTEX_RESOLVE_LOCATOR 澶辫触: {result.get("err", "")}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def cmd_cortex_tap_locator():
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    locator = data.get('locator') or {}
    if not isinstance(locator, dict) or not locator:
        return jsonify({'success': False, 'message': 'locator is required'}), 400

    try:
        result = client.cortex_tap_locator(locator)
        ok = bool(result.get('ok'))
        msg = 'CORTEX_TAP_LOCATOR 鎴愬姛' if ok else f'CORTEX_TAP_LOCATOR 澶辫触: {result.get("err", "")}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/cortex_trace_pull', methods=['POST'])
@app.route('/api/command/trace_pull', methods=['POST'])
def cmd_cortex_trace_pull():
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    max_lines = int(data.get('max_lines', 200))
    max_lines = max(1, min(max_lines, 1000))

    try:
        trace = client.cortex_trace_pull(max_lines=max_lines)
        lines = [x for x in trace.splitlines() if x.strip()]
        return jsonify({
            'success': True,
            'message': f'CORTEX_TRACE_PULL ok: {len(lines)} lines',
            'response': {
                'max_lines': max_lines,
                'line_count': len(lines),
                'trace': trace
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def cmd_cortex_route_run():
    """Run device-side route_run via map from home/start_page to target_page."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    package_name = (data.get('package') or '').strip()
    target_page = (data.get('target_page') or '').strip()
    start_page = (data.get('start_page') or '').strip()
    try:
        max_steps = int(data.get('max_steps', 16) or 16)
    except Exception:
        max_steps = 16
    max_steps = max(1, min(int(max_steps), 128))

    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400
    if not target_page:
        return jsonify({'success': False, 'message': 'target_page is required'}), 400

    try:
        if start_page:
            result = client.cortex_route_run(package_name, target_page, max_steps=max_steps, start_page=start_page)
        else:
            result = client.cortex_route_run(package_name, target_page, max_steps=max_steps)
        ok = bool(result.get('ok'))
        steps = result.get('steps') or []
        if ok:
            msg = f'CORTEX_ROUTE_RUN 鎴愬姛: {result.get("from_page")} -> {result.get("to_page")}, steps={len(steps)}'
        else:
            reason = result.get('reason', '') or 'unknown'
            msg = f'CORTEX_ROUTE_RUN 澶辫触: {reason}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


    data = request.json or {}
    package_name = (data.get('package') or '').strip()
    from_page = (data.get('from_page') or '').strip()
    to_page = (data.get('to_page') or '').strip()
    try:
        max_steps = int(data.get('max_steps', 16) or 16)
    except Exception:
        max_steps = 16
    max_steps = max(1, min(int(max_steps), 128))

    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400
    if not from_page or not to_page:
        return jsonify({'success': False, 'message': 'from_page 鍜?to_page 鍧囦负蹇呭～'}), 400

    try:
        result = client.cortex_route_run(package_name, from_page, to_page, max_steps=max_steps)
        ok = bool(result.get('ok'))
        steps = result.get('steps') or []
        if ok:
            msg = f'CORTEX_ROUTE_RUN 鎴愬姛: {from_page} -> {to_page}, steps={len(steps)}'
        else:
            reason = result.get('reason', '') or 'unknown'
            msg = f'CORTEX_ROUTE_RUN 澶辫触: {reason}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Lifecycle Layer (0x40-0x4F)
# =============================================================================

@app.route('/api/command/launch_app', methods=['POST'])
def cmd_launch_app():
    """鍙戦€?LAUNCH_APP 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    package_name = data.get('package', '')
    clear_task = data.get('clear_task', False)

    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    try:
        success = client.launch_app(package_name, clear_task=clear_task)
        return jsonify({
            'success': success,
            'message': f'鍚姩 {package_name} 鎴愬姛' if success else f'鍚姩 {package_name} 澶辫触'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/stop_app', methods=['POST'])
def cmd_stop_app():
    """鍙戦€?STOP_APP 鍛戒护"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json
    package_name = data.get('package', '')

    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    try:
        success = client.stop_app(package_name)
        return jsonify({
            'success': success,
            'message': f'鍋滄 {package_name} 鎴愬姛' if success else f'鍋滄 {package_name} 澶辫触'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/list_apps', methods=['POST'])
def cmd_list_apps():
    """?????????"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    filter_type = data.get('filter', 'user')  # user / system / all

    try:
        raw_apps = client.list_apps(filter_type)
        apps_with_names = _normalize_installed_apps(raw_apps)

        return jsonify({
            'success': True,
            'message': f'????????: {len(apps_with_names)} ???',
            'response': {
                'filter': filter_type,
                'count': len(apps_with_names),
                'apps': apps_with_names
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/command/system_control', methods=['POST'])
def cmd_system_control():
    """Run generic system control action via CMD_SYSTEM_CONTROL."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    action = str(data.get('action') or '').strip()
    if not action:
        return jsonify({'success': False, 'message': 'action is required'}), 400

    params = data.get('params')
    merged = {}
    if isinstance(params, dict):
        merged.update(params)

    # Also accept flat payload fields for convenience.
    for k, v in data.items():
        if k in ('action', 'params'):
            continue
        merged[k] = v

    try:
        result = client.system_control(action, params=merged)
        ok = bool(result.get('ok'))
        msg = f'SYSTEM_CONTROL {action}: {"ok" if ok else "failed"}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# =============================================================================
# Media Layer (0x60-0x6F)
# =============================================================================

@app.route('/api/command/screenshot', methods=['POST'])
def cmd_screenshot():
    """鍙戦€?SCREENSHOT 鍛戒护 (浣跨敤鍒嗙墖浼犺緭)"""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        # 浣跨敤鍒嗙墖浼犺緭鏂瑰紡鑾峰彇鎴浘
        image_data = client.request_screenshot()

        if image_data and len(image_data) > 0:
            # Screenshot OK: return base64 image to frontend preview.
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            return jsonify({
                'success': True,
                'message': f'鎴浘鎴愬姛: {len(image_data)} 瀛楄妭 ({len(image_data)/1024:.1f} KB)',
                'response': {
                    'size': len(image_data),
                    'image': image_base64
                }
            })
        else:
            return jsonify({
                'success': False,
                'message': 'screenshot failed: empty data'
            })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/command/screenshot/raw', methods=['GET'])
def cmd_screenshot_raw():
    """Return raw screenshot bytes."""
    if not client:
        return Response('not connected', status=400, mimetype='text/plain')

    try:
        # 浣跨敤鍒嗙墖浼犺緭鏂瑰紡鑾峰彇鎴浘
        image_data = client.request_screenshot()

        if image_data and len(image_data) > 0:
            # 杩斿洖 JPEG 鍥剧墖 (鏈嶅姟绔凡鍘嬬缉涓?JPEG)
            return Response(image_data, mimetype='image/jpeg')
        else:
            return Response('鎴浘澶辫触', status=500, mimetype='text/plain')
    except Exception as e:
        return Response(str(e), status=500, mimetype='text/plain')


# =============================================================================
# Auto Map Builder v2/v3
# =============================================================================

# 妫€娴?VLM 鏄惁鍙敤
VLM_AVAILABLE = False
try:
    from map_builder import (
        NodeMapBuilder,
        ExplorationConfig
    )
    from map_builder.vlm_engine import VLMEngine
    VLM_AVAILABLE = True
except ImportError as e:
    print(f"[app.py] Auto Map Builder 涓嶅彲鐢? {e}")

# 鍏ㄥ眬鎺㈢储鍣ㄥ疄渚嬪拰鐘舵€?explorer_instance = None
exploration_result = None
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__))))
MAP_REPO_DEFAULT_ROOT = os.path.join(PROJECT_ROOT, 'map_repo')
MAP_PUBLISH_DEFAULT_REPO = os.getenv('MAP_PUBLISH_REPO', 'wuwei-crg/LXB-MapRepo').strip()
MAP_PUBLISH_DEFAULT_BASE_BRANCH = os.getenv('MAP_PUBLISH_BASE_BRANCH', 'main').strip() or 'main'
MAP_PUBLISH_DEFAULT_LANE = os.getenv('MAP_PUBLISH_DEFAULT_LANE', 'candidates').strip() or 'candidates'
MAP_PUBLISH_DEFAULT_MAPS_ROOT = os.getenv('MAP_PUBLISH_MAPS_ROOT', f'{MAP_PUBLISH_DEFAULT_LANE}/maps').strip() or f'{MAP_PUBLISH_DEFAULT_LANE}/maps'
MAP_PUBLISH_GITHUB_TOKEN = os.getenv('MAP_PUBLISH_GITHUB_TOKEN', '').strip()
MAP_PUBLISH_DEFAULT_MODE = os.getenv('MAP_PUBLISH_MODE', 'local_git').strip().lower() or 'local_git'
EXPLORATION_LOCK = threading.RLock()
QUEUE_LOCK = threading.RLock()
QUEUE_THREAD = None


def _new_queue_state() -> dict:
    return {
        'mode': 'node_queue',
        'run_id': None,
        'running': False,
        'stopping': False,
        'retry_max': 2,
        'pending': [],
        'current': None,
        'completed': [],
        'failed': [],
        'attempts': {},
        'started_at': None,
        'finished_at': None,
        'output_root': 'map_repo',
    }


exploration_status = {
    'running': False,
    'package': None,
    'version': 'v2',  # v2 鎴?v3
    'progress': {
        'pages_discovered': 0,
        'nodes_discovered': 0,
        'current_page': None
    },
    'result': None,
    'logs': [],
    'queue': _new_queue_state(),
}


def _append_explore_log(level: str, message: str, log_data=None) -> None:
    with EXPLORATION_LOCK:
        logs = exploration_status.setdefault('logs', [])
        logs.append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'level': str(level or 'info'),
            'message': str(message or ''),
            'data': log_data
        })
        if len(logs) > 5000:
            del logs[:len(logs) - 5000]


def _safe_package_dir_name(package_name: str) -> str:
    value = str(package_name or '').strip()
    if not value:
        return 'unknown'
    return value.replace('/', '_').replace('\\', '_')


def _resolve_map_repo_root(output_root: Optional[str] = None) -> str:
    root = str(output_root or '').strip()
    if not root:
        root = MAP_REPO_DEFAULT_ROOT
    elif not os.path.isabs(root):
        root = os.path.abspath(os.path.join(PROJECT_ROOT, root))
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, 'maps'), exist_ok=True)
    return root


def _map_repo_index_path(repo_root: str) -> str:
    return os.path.join(repo_root, 'index.json')


def _new_map_repo_index() -> dict:
    return {
        'schema_version': 'lxb.maps.index.v1',
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'maps': []
    }


def _load_map_repo_index(repo_root: str) -> dict:
    index_path = _map_repo_index_path(repo_root)
    if not os.path.exists(index_path):
        return _new_map_repo_index()
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            if not isinstance(loaded.get('maps'), list):
                loaded['maps'] = []
            if not loaded.get('schema_version'):
                loaded['schema_version'] = 'lxb.maps.index.v1'
            return loaded
    except Exception:
        pass
    return _new_map_repo_index()


def _save_map_repo_index(repo_root: str, index_obj: dict) -> str:
    index_path = _map_repo_index_path(repo_root)
    index_obj = dict(index_obj or {})
    index_obj['schema_version'] = index_obj.get('schema_version') or 'lxb.maps.index.v1'
    index_obj['updated_at'] = datetime.now(timezone.utc).isoformat()
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_obj, f, ensure_ascii=False, indent=2)
    return index_path


def _save_nav_map_to_repo(
        package_name: str,
        nav_map_obj: dict,
        run_stats: Optional[dict] = None,
        config_snapshot: Optional[dict] = None,
        source: str = 'manual',
        output_root: Optional[str] = None
) -> dict:
    repo_root = _resolve_map_repo_root(output_root)
    pkg_dir = _safe_package_dir_name(package_name)
    generated_at = datetime.now(timezone.utc).isoformat()

    nav_json_text = json.dumps(nav_map_obj or {}, ensure_ascii=False, indent=2)
    nav_json_bytes = nav_json_text.encode('utf-8')
    gz_bytes = gzip.compress(nav_json_bytes, compresslevel=6)
    sha256_hex = hashlib.sha256(gz_bytes).hexdigest()
    map_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{sha256_hex[:8]}"

    rel_dir = os.path.join('maps', pkg_dir, map_id).replace('\\', '/')
    abs_dir = os.path.join(repo_root, 'maps', pkg_dir, map_id)
    os.makedirs(abs_dir, exist_ok=True)

    rel_map_path = f"{rel_dir}/nav_map.json.gz"
    rel_meta_path = f"{rel_dir}/meta.json"
    abs_map_path = os.path.join(abs_dir, 'nav_map.json.gz')
    abs_meta_path = os.path.join(abs_dir, 'meta.json')

    with open(abs_map_path, 'wb') as f:
        f.write(gz_bytes)

    meta_obj = {
        'schema_version': 'lxb.map.meta.v1',
        'package': package_name,
        'map_id': map_id,
        'generated_at': generated_at,
        'source': source,
        'builder': {
            'name': 'LXB-MapBuilder',
            'track': 'node_v5',
        },
        'config': config_snapshot or {},
        'stats': run_stats or {},
        'artifacts': {
            'map_path': rel_map_path,
            'sha256': sha256_hex,
            'bytes_gzip': len(gz_bytes),
            'bytes_json': len(nav_json_bytes),
        }
    }
    with open(abs_meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta_obj, f, ensure_ascii=False, indent=2)

    index_obj = _load_map_repo_index(repo_root)
    rows = list(index_obj.get('maps') or [])
    rows = [
        row for row in rows
        if not (str(row.get('package')) == package_name and str(row.get('map_id')) == map_id)
    ]
    rows.append({
        'package': package_name,
        'map_id': map_id,
        'generated_at': generated_at,
        'map_path': rel_map_path,
        'meta_path': rel_meta_path,
        'sha256': sha256_hex,
        'bytes': len(gz_bytes),
        'stats': run_stats or {},
    })
    rows.sort(key=lambda x: str(x.get('generated_at') or ''), reverse=True)
    index_obj['maps'] = rows
    _save_map_repo_index(repo_root, index_obj)

    return {
        'repo_root': repo_root,
        'package': package_name,
        'map_id': map_id,
        'generated_at': generated_at,
        'map_path': abs_map_path,
        'map_path_rel': rel_map_path,
        'meta_path': abs_meta_path,
        'meta_path_rel': rel_meta_path,
        'sha256': sha256_hex,
        'bytes': len(gz_bytes),
    }


def _snapshot_node_config(data: dict) -> dict:
    return {
        'max_pages': int(data.get('max_pages', 30)),
        'max_depth': int(data.get('max_depth', 3)),
        'max_time_seconds': int(data.get('max_time_seconds', 1800)),
        'action_delay_ms': int(data.get('action_delay_ms', 800)),
        'explore_mode': str(data.get('explore_mode', 'serial')),
        'click_delay': float(data.get('click_delay', 1.5)),
    }


def _load_json_or_gz(abs_filepath: str):
    if abs_filepath.lower().endswith('.json.gz'):
        with gzip.open(abs_filepath, 'rt', encoding='utf-8') as f:
            return json.load(f)
    with open(abs_filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def _safe_path_under_root(root: str, candidate_path: str) -> str:
    root_abs = os.path.abspath(root)
    candidate_abs = os.path.abspath(candidate_path)
    if not candidate_abs.startswith(root_abs):
        raise RuntimeError('illegal_path')
    return candidate_abs


def _normalize_repo_full_name(repo: str) -> str:
    value = str(repo or '').strip().strip('/')
    if not value:
        raise RuntimeError('repo is required')
    if not re.match(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', value):
        raise RuntimeError('invalid repo format, expected owner/repo')
    return value


def _safe_branch_part(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not raw:
        return 'unknown'
    raw = re.sub(r'[^a-z0-9._-]+', '-', raw)
    raw = raw.strip('-.')
    return raw or 'unknown'


def _normalize_publish_lane(value: str) -> str:
    lane = str(value or '').strip().lower()
    if lane in ('stable', 'candidates'):
        return lane
    return MAP_PUBLISH_DEFAULT_LANE


def _normalize_publish_mode(value: str) -> str:
    mode = str(value or '').strip().lower()
    if mode in ('github_api', 'local_git'):
        return mode
    return MAP_PUBLISH_DEFAULT_MODE if MAP_PUBLISH_DEFAULT_MODE in ('github_api', 'local_git') else 'local_git'


def _lane_default_maps_root(lane: str) -> str:
    return f'{lane}/maps'


def _run_git(repo_root: str, args: list, timeout_sec: int = 40) -> str:
    cmd = ['git', '-C', repo_root] + [str(a) for a in (args or [])]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout_sec
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or '').strip()
        stdout = (proc.stdout or '').strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr or stdout or f'code={proc.returncode}'}")
    return (proc.stdout or '').strip()


def _resolve_local_git_repo_root(value: str) -> str:
    root = str(value or '').strip()
    if not root:
        root = _resolve_map_repo_root()
    elif not os.path.isabs(root):
        root = os.path.abspath(os.path.join(PROJECT_ROOT, root))
    if not os.path.isdir(root):
        raise RuntimeError(f'local git repo not found: {root}')
    git_dir = os.path.join(root, '.git')
    if not os.path.isdir(git_dir):
        raise RuntimeError(f'not a git repo: {root}')
    return root


def _extract_repo_from_remote(remote_url: str) -> str:
    v = str(remote_url or '').strip()
    if not v:
        return ''
    # git@github.com:owner/repo.git
    m = re.match(r'^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$', v)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # https://github.com/owner/repo.git
    m = re.match(r'^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', v)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ''


def _is_gzip_bytes(data: bytes) -> bool:
    return bool(data and len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B)


def _load_nav_map_obj_from_bytes(raw_bytes: bytes, filename_hint: str = '') -> dict:
    if raw_bytes is None:
        raise RuntimeError('empty map bytes')
    use_gzip = _is_gzip_bytes(raw_bytes) or str(filename_hint or '').lower().endswith('.gz')
    try:
        if use_gzip:
            text = gzip.decompress(raw_bytes).decode('utf-8')
        else:
            text = raw_bytes.decode('utf-8')
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError(f'invalid map json/json.gz: {e}')
    if not isinstance(obj, dict):
        raise RuntimeError('map content must be a JSON object')
    return obj


def _load_meta_obj_from_bytes(raw_bytes: bytes, filename_hint: str = '') -> dict:
    if raw_bytes is None:
        raise RuntimeError('empty meta bytes')
    use_gzip = _is_gzip_bytes(raw_bytes) or str(filename_hint or '').lower().endswith('.gz')
    try:
        if use_gzip:
            text = gzip.decompress(raw_bytes).decode('utf-8')
        else:
            text = raw_bytes.decode('utf-8')
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError(f'invalid meta json/json.gz: {e}')
    if not isinstance(obj, dict):
        raise RuntimeError('meta content must be a JSON object')
    return obj


def _extract_package_from_nav_map(nav_obj: dict) -> str:
    if not isinstance(nav_obj, dict):
        return ''
    for key in ('package', 'package_name', 'app_package'):
        value = str(nav_obj.get(key) or '').strip()
        if value:
            return value
    meta = nav_obj.get('meta')
    if isinstance(meta, dict):
        for key in ('package', 'package_name', 'app_package'):
            value = str(meta.get(key) or '').strip()
            if value:
                return value
    return ''


def _github_api_request(method: str, api_path: str, token: str, payload: Optional[dict] = None):
    if not token:
        raise RuntimeError('github token is required')
    path = str(api_path or '').strip()
    if not path:
        raise RuntimeError('empty github api path')
    url = path if path.startswith('http://') or path.startswith('https://') else f'https://api.github.com{path}'
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url=url, data=body, method=str(method or 'GET').upper())
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    if payload is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            if not content:
                return {}
            return json.loads(content.decode('utf-8'))
    except urllib.error.HTTPError as e:
        body_text = ''
        try:
            body_text = (e.read() or b'').decode('utf-8', errors='replace')
        except Exception:
            body_text = ''
        message = body_text
        try:
            body_obj = json.loads(body_text) if body_text else {}
            message = str((body_obj or {}).get('message') or body_text or f'HTTP {e.code}')
        except Exception:
            message = body_text or f'HTTP {e.code}'
        raise RuntimeError(f'github_api_http_{e.code}:{message}')
    except Exception as e:
        raise RuntimeError(f'github_api_error:{e}')


def _github_get_repo(repo: str, token: str) -> dict:
    return _github_api_request('GET', f'/repos/{repo}', token)


def _github_get_branch_sha(repo: str, branch: str, token: str) -> str:
    ref = _github_api_request('GET', f'/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe="")}', token)
    obj = ref.get('object') if isinstance(ref, dict) else {}
    sha = str((obj or {}).get('sha') or '').strip()
    if not sha:
        raise RuntimeError(f'cannot read sha for branch {branch}')
    return sha


def _github_create_branch(repo: str, branch: str, from_sha: str, token: str) -> None:
    _github_api_request('POST', f'/repos/{repo}/git/refs', token, {
        'ref': f'refs/heads/{branch}',
        'sha': from_sha,
    })


def _github_put_file(repo: str, path: str, branch: str, token: str, content_bytes: bytes, message: str, sha: Optional[str] = None):
    encoded = urllib.parse.quote(path, safe='/')
    payload = {
        'message': message,
        'branch': branch,
        'content': base64.b64encode(content_bytes).decode('ascii'),
    }
    if sha:
        payload['sha'] = sha
    return _github_api_request('PUT', f'/repos/{repo}/contents/{encoded}', token, payload)


def _github_create_pr(repo: str, title: str, head: str, base: str, body: str, token: str) -> dict:
    return _github_api_request('POST', f'/repos/{repo}/pulls', token, {
        'title': title,
        'head': head,
        'base': base,
        'body': body,
    })


def _publish_to_repo_via_local_git(
        local_repo_root: str,
        base_branch: str,
        branch_name: str,
        map_rel_path: str,
        meta_rel_path: str,
        nav_gz_bytes: bytes,
        meta_obj: dict,
        commit_prefix: str
) -> dict:
    status = _run_git(local_repo_root, ['status', '--porcelain'])
    if status.strip():
        raise RuntimeError('local git repo has uncommitted changes, please clean it first')

    _run_git(local_repo_root, ['fetch', 'origin', base_branch], timeout_sec=80)
    _run_git(local_repo_root, ['checkout', '-B', branch_name, f'origin/{base_branch}'])

    map_abs = _safe_path_under_root(local_repo_root, os.path.join(local_repo_root, map_rel_path.replace('/', os.sep)))
    meta_abs = _safe_path_under_root(local_repo_root, os.path.join(local_repo_root, meta_rel_path.replace('/', os.sep)))

    os.makedirs(os.path.dirname(map_abs), exist_ok=True)
    os.makedirs(os.path.dirname(meta_abs), exist_ok=True)

    with open(map_abs, 'wb') as f:
        f.write(nav_gz_bytes)

    with open(meta_abs, 'w', encoding='utf-8') as f:
        json.dump(meta_obj, f, ensure_ascii=False, indent=2)

    _run_git(local_repo_root, ['add', '--', map_rel_path, meta_rel_path])
    _run_git(local_repo_root, ['commit', '-m', f'{commit_prefix} local git publish'])
    _run_git(local_repo_root, ['push', '-u', 'origin', branch_name], timeout_sec=120)

    remote_url = _run_git(local_repo_root, ['remote', 'get-url', 'origin'])
    inferred_repo = _extract_repo_from_remote(remote_url)
    return {
        'remote_url': remote_url,
        'inferred_repo': inferred_repo,
    }


def _legacy_map_builder_disabled_response():
    return jsonify({
        'success': False,
        'message': 'legacy auto_map_builder strategies are archived; use /api/explore/node/start'
    }), 410


@app.route('/api/explore/start', methods=['POST'])
def explore_start():
    return _legacy_map_builder_disabled_response()
    """鍚姩搴旂敤鎺㈢储 (v2 VLM+XML 铻嶅悎)"""
    global client, explorer_instance, exploration_result, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    if exploration_status['running']:
        return jsonify({'success': False, 'message': 'exploration is already running'}), 400

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'Auto Map Builder module unavailable'}), 400

    data = request.json
    package_name = data.get('package', '')

    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    try:
        from datetime import datetime

        # 鍒涘缓閰嶇疆
        config = ExplorationConfig(
            max_pages=data.get('max_pages', 50),
            max_depth=data.get('max_depth', 10),
            max_time_seconds=data.get('max_time_seconds', 1800),
            enable_od=data.get('enable_od', True),
            enable_ocr=data.get('enable_ocr', True),
            enable_caption=data.get('enable_caption', True),
            # 骞跺彂鎺ㄧ悊閰嶇疆
            vlm_concurrent_enabled=data.get('vlm_concurrent_enabled', False),
            vlm_concurrent_requests=data.get('vlm_concurrent_requests', 5),
            vlm_occurrence_threshold=data.get('vlm_occurrence_threshold', 2),
            iou_threshold=data.get('iou_threshold', 0.5),
            action_delay_ms=data.get('action_delay_ms', 1000),
            scroll_enabled=data.get('scroll_enabled', True),
            max_scrolls_per_page=data.get('max_scrolls_per_page', 5),
            save_screenshots=data.get('save_screenshots', True),
            output_dir=data.get('output_dir', './maps')
        )

        # 鏃ュ織鍥炶皟
        def log_callback(level, message, log_data=None):
            log_entry = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'level': level,
                'message': message,
                'data': log_data
            }
            exploration_status['logs'].append(log_entry)

        # 娓呯┖鏃ュ織
        exploration_status['logs'] = []

        # 鍒涘缓鎺㈢储鍣?        explorer_instance = AutoMapBuilder(client, config, log_callback)

        # 鏇存柊鐘舵€?        exploration_status['running'] = True
        exploration_status['package'] = package_name
        exploration_status['progress'] = {
            'pages_discovered': 0,
            'nodes_discovered': 0,
            'current_page': None
        }
        exploration_status['result'] = None

        log_callback('info', f'寮€濮嬫帰绱? {package_name}')

        # 鎵ц鎺㈢储
        exploration_result = explorer_instance.explore(package_name)

        # 鏇存柊缁撴灉
        exploration_status['running'] = False
        exploration_status['progress'] = {
            'pages_discovered': exploration_result.page_count,
            'nodes_discovered': sum(len(p.nodes) for p in exploration_result.pages.values()),
            'current_page': 'completed'
        }
        exploration_status['result'] = {
            'pages': exploration_result.page_count,
            'transitions': exploration_result.transition_count,
            'time': round(exploration_result.exploration_time_seconds, 2),
            'vlm_inferences': exploration_result.vlm_inference_count,
            'vlm_time_ms': round(exploration_result.vlm_total_time_ms, 2)
        }

        return jsonify({
            'success': True,
            'message': 'message',
            'result': exploration_status['result']
        })

    except Exception as e:
        import traceback
        exploration_status['running'] = False
        return jsonify({
            'success': False,
            'message': f'鎺㈢储澶辫触: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/status', methods=['GET'])
def explore_status():
    """Get exploration status."""
    global explorer_instance

    queue_running = False
    with QUEUE_LOCK:
        queue_state = dict(exploration_status.get('queue') or _new_queue_state())
        queue_running = bool(queue_state.get('running'))

    if explorer_instance:
        try:
            from map_builder import ExplorationStatus
            actual_status = explorer_instance.status
            with EXPLORATION_LOCK:
                exploration_status['status'] = actual_status.value
                if not queue_running:
                    exploration_status['running'] = actual_status == ExplorationStatus.RUNNING
                exploration_status['paused'] = actual_status == ExplorationStatus.PAUSED
        except Exception:
            pass

    with EXPLORATION_LOCK:
        snapshot = dict(exploration_status)
        snapshot['queue'] = dict(exploration_status.get('queue') or _new_queue_state())
    return jsonify(snapshot)


@app.route('/api/explore/pause', methods=['POST'])
def explore_pause():
    """鏆傚仠鎺㈢储"""
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        explorer_instance.pause()
        return jsonify({
            'success': True,
            'message': 'message',
            'status': explorer_instance.status.value
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/explore/resume', methods=['POST'])
def explore_resume():
    """鎭㈠鎺㈢储"""
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        explorer_instance.resume()
        return jsonify({
            'success': True,
            'message': 'message',
            'status': explorer_instance.status.value
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/explore/stop', methods=['POST'])
def explore_stop():
    """Stop single exploration or queue exploration."""
    global explorer_instance, exploration_status

    queue_running = False
    with QUEUE_LOCK:
        queue_state = exploration_status.get('queue') or _new_queue_state()
        queue_running = bool(queue_state.get('running'))
        if queue_running:
            queue_state['stopping'] = True
            exploration_status['queue'] = queue_state

    if not explorer_instance and not queue_running:
        return jsonify({'success': False, 'message': 'no running exploration'}), 400

    try:
        if explorer_instance:
            explorer_instance.stop()
        if queue_running:
            _append_explore_log('warn', '[queue] stop requested by user')
        return jsonify({
            'success': True,
            'message': 'stop requested',
            'queue_running': queue_running,
            'status': explorer_instance.status.value if explorer_instance else 'idle'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/explore/logs', methods=['GET'])
def explore_logs():
    """鑾峰彇鎺㈢储鏃ュ織"""
    since = request.args.get('since', 0, type=int)
    logs = exploration_status.get('logs', [])
    return jsonify({
        'success': True,
        'logs': logs[since:],
        'total': len(logs)
    })


@app.route('/api/explore/realtime', methods=['GET'])
def explore_realtime():
    """鑾峰彇瀹炴椂鎺㈢储鐘舵€侊紙鐢ㄤ簬鍙鍖栵級"""
    global explorer_instance

    if not explorer_instance:
        return jsonify({
            'success': False,
            'message': 'message',
        }), 400

    try:
        # v3 浣跨敤 get_realtime_state 鏂规硶
        if hasattr(explorer_instance, 'get_realtime_state'):
            realtime_state = explorer_instance.get_realtime_state()
        # v2 浣跨敤 _explorer.get_realtime_state
        elif hasattr(explorer_instance, '_explorer') and explorer_instance._explorer:
            realtime_state = explorer_instance._explorer.get_realtime_state()
        else:
            realtime_state = {}

        return jsonify({
            'success': True,
            'data': realtime_state
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500


# =============================================================================
# Auto Map Builder v3 - 璇箟鎺㈢储 API
# =============================================================================

@app.route('/api/explore/v3/start', methods=['POST'])
def explore_v3_start():
    return _legacy_map_builder_disabled_response()
    """鍚姩 v3 璇箟鎺㈢储"""
    global client, explorer_instance, exploration_result, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    if exploration_status['running']:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json
    package_name = data.get('package', '')

    if not package_name:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from datetime import datetime

        # 鍒涘缓閰嶇疆
        config = ExplorationConfig(
            max_pages=data.get('max_pages', 30),
            max_depth=data.get('max_depth', 5),
            max_time_seconds=data.get('max_time_seconds', 1800),
            action_delay_ms=data.get('action_delay_ms', 800),
            output_dir=data.get('output_dir', './maps')
        )

        # 鏃ュ織鍥炶皟
        def log_callback(level, message, log_data=None):
            log_entry = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'level': level,
                'message': message,
                'data': log_data
            }
            exploration_status['logs'].append(log_entry)

        # 娓呯┖鏃ュ織
        exploration_status['logs'] = []

        # 鍒涘缓 v3 鎺㈢储鍣?        explorer_instance = SemanticMapBuilder(client, config, log_callback)

        # 鏇存柊鐘舵€?        exploration_status['running'] = True
        exploration_status['package'] = package_name
        exploration_status['version'] = 'v3'
        exploration_status['progress'] = {
            'pages_discovered': 0,
            'transitions_discovered': 0,
            'current_page': None
        }
        exploration_status['result'] = None

        log_callback('info', f'[v3] 寮€濮嬭涔夋帰绱? {package_name}')

        # 鎵ц鎺㈢储
        exploration_result = explorer_instance.explore(package_name)

        # 鏇存柊缁撴灉
        exploration_status['running'] = False
        exploration_status['progress'] = {
            'pages_discovered': len(exploration_result.graph.pages),
            'transitions_discovered': len(exploration_result.graph.transitions),
            'current_page': 'completed'
        }
        exploration_status['result'] = {
            'pages': len(exploration_result.graph.pages),
            'transitions': len(exploration_result.graph.transitions),
            'time': round(exploration_result.exploration_time_seconds, 2),
            'vlm_inferences': exploration_result.vlm_inference_count,
            'vlm_time_ms': round(exploration_result.vlm_total_time_ms, 2),
            'actions': exploration_result.total_actions
        }

        return jsonify({
            'success': True,
            'message': 'message',
            'result': exploration_status['result']
        })

    except Exception as e:
        import traceback
        exploration_status['running'] = False
        return jsonify({
            'success': False,
            'message': f'鎺㈢储澶辫触: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/v3/graph', methods=['GET'])
def explore_v3_graph():
    """Get v3 navigation graph."""
    return _legacy_map_builder_disabled_response()
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    # 妫€鏌ユ槸鍚︽槸 v3 鎺㈢储鍣?    if not hasattr(explorer_instance, 'graph') or explorer_instance.graph is None:
        return jsonify({'success': False, 'message': '褰撳墠涓嶆槸 v3 鎺㈢储鎴栨病鏈夊鑸浘'}), 400

    try:
        graph = explorer_instance.graph

        # 搴忓垪鍖栭〉闈?        pages = []
        for page in graph.pages.values():
            anchors = []
            for anchor in page.nav_anchors:
                anchors.append({
                    'anchor_id': anchor.anchor_id,
                    'role': anchor.role,
                    'description': anchor.description,
                    'locator': {
                        'resource_id': anchor.locator.resource_id,
                        'text': anchor.locator.text,
                        'bounds': list(anchor.locator.bounds) if anchor.locator.bounds else None
                    }
                })

            pages.append({
                'semantic_id': page.semantic_id,
                'page_type': page.page_type,
                'sub_state': page.sub_state,
                'activity': page.activity,
                'description': page.description,
                'nav_anchors': anchors
            })

        # 搴忓垪鍖栬烦杞?        transitions = []
        for trans in graph.transitions:
            transitions.append({
                'from_page': trans.from_page,
                'to_page': trans.to_page,
                'anchor_id': trans.anchor_id,
                'locator': {
                    'resource_id': trans.locator.resource_id if trans.locator else None,
                    'text': trans.locator.text if trans.locator else None,
                    'bounds': list(trans.locator.bounds) if trans.locator and trans.locator.bounds else None
                }
            })

        return jsonify({
            'success': True,
            'data': {
                'pages': pages,
                'transitions': transitions,
                'page_count': len(pages),
                'transition_count': len(transitions)
            }
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/v3/save', methods=['POST'])
def explore_v3_save():
    """Save v3 navigation graph."""
    return _legacy_map_builder_disabled_response()
    global explorer_instance, exploration_result

    if not explorer_instance:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    if not hasattr(explorer_instance, 'save'):
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json or {}
    filepath = data.get('filepath')

    try:
        explorer_instance.save(filepath)

        return jsonify({
            'success': True,
            'message': f'瀵艰埅鍥惧凡淇濆瓨',
            'filepath': filepath
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/v3/find_path', methods=['POST'])
def explore_v3_find_path():
    return _legacy_map_builder_disabled_response()
    """鏌ユ壘璺緞"""
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    if not hasattr(explorer_instance, 'find_path'):
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json
    from_page = data.get('from_page', '')
    to_page = data.get('to_page', '')

    if not from_page or not to_page:
        return jsonify({'success': False, 'message': '璇锋寚瀹氳捣濮嬪拰鐩爣椤甸潰'}), 400

    try:
        path = explorer_instance.find_path(from_page, to_page)

        if path is None:
            return jsonify({
                'success': False,
                'message': 'message',
            })

        # 搴忓垪鍖栬矾寰?        path_data = []
        for trans in path:
            path_data.append({
                'from_page': trans.from_page,
                'to_page': trans.to_page,
                'anchor_id': trans.anchor_id
            })

        return jsonify({
            'success': True,
            'message': 'message',
            'data': {
                'path': path_data,
                'steps': len(path)
            }
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/v3/navigate', methods=['POST'])
def explore_v3_navigate():
    return _legacy_map_builder_disabled_response()
    """鎵ц瀵艰埅"""
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    if not hasattr(explorer_instance, 'navigate_to'):
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json
    target_page = data.get('target_page', '')
    verify = data.get('verify', True)

    if not target_page:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        success, message = explorer_instance.navigate_to(target_page, verify=verify)

        return jsonify({
            'success': success,
            'message': message
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


# =============================================================================
# Auto Map Builder SoM (Set-of-Mark) API
# =============================================================================

@app.route('/api/explore/som/start', methods=['POST'])
def explore_som_start():
    return _legacy_map_builder_disabled_response()
    """鍚姩 SoM 鎺㈢储锛堟帹鑽愶級"""
    global client, explorer_instance, exploration_result, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    if exploration_status['running']:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json
    package_name = data.get('package', '')

    if not package_name:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from datetime import datetime

        # 鍒涘缓閰嶇疆
        config = ExplorationConfig(
            max_pages=data.get('max_pages', 30),
            max_depth=data.get('max_depth', 5),
            max_time_seconds=data.get('max_time_seconds', 1800),
            action_delay_ms=data.get('action_delay_ms', 800),
            output_dir=data.get('output_dir', './maps')
        )

        # 鏃ュ織鍥炶皟
        def log_callback(level, message, log_data=None):
            log_entry = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'level': level,
                'message': message,
                'data': log_data
            }
            exploration_status['logs'].append(log_entry)

        # 娓呯┖鏃ュ織
        exploration_status['logs'] = []

        # 鍒涘缓 SoM 鎺㈢储鍣?        explorer_instance = SoMMapBuilder(client, config, log_callback)

        # 鏇存柊鐘舵€?        exploration_status['running'] = True
        exploration_status['package'] = package_name
        exploration_status['version'] = 'som'
        exploration_status['progress'] = {
            'pages_discovered': 0,
            'transitions_discovered': 0,
            'current_page': None
        }
        exploration_status['result'] = None

        log_callback('info', f'[SoM] 寮€濮嬫帰绱? {package_name}')

        # 鎵ц鎺㈢储
        exploration_result = explorer_instance.explore(package_name)

        # 鏇存柊缁撴灉
        exploration_status['running'] = False
        exploration_status['progress'] = {
            'pages_discovered': len(exploration_result.graph.pages),
            'transitions_discovered': len(exploration_result.graph.transitions),
            'current_page': 'completed'
        }
        exploration_status['result'] = {
            'pages': len(exploration_result.graph.pages),
            'transitions': len(exploration_result.graph.transitions),
            'time': round(exploration_result.exploration_time_seconds, 2),
            'vlm_inferences': exploration_result.vlm_inference_count,
            'vlm_time_ms': round(exploration_result.vlm_total_time_ms, 2),
            'actions': exploration_result.total_actions
        }

        return jsonify({
            'success': True,
            'message': 'message',
            'result': exploration_status['result']
        })

    except Exception as e:
        import traceback
        exploration_status['running'] = False
        return jsonify({
            'success': False,
            'message': f'鎺㈢储澶辫触: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/coord/start', methods=['POST'])
def explore_coord_start():
    return _legacy_map_builder_disabled_response()
    """鍚姩鍧愭爣椹卞姩鎺㈢储 (v4 鎺ㄨ崘)"""
    global client, explorer_instance, exploration_result, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    if exploration_status['running']:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    data = request.json
    package_name = data.get('package', '')

    if not package_name:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from datetime import datetime

        config = ExplorationConfig(
            max_pages=data.get('max_pages', 30),
            max_depth=data.get('max_depth', 5),
            max_time_seconds=data.get('max_time_seconds', 1800),
            action_delay_ms=data.get('action_delay_ms', 800),
            output_dir=data.get('output_dir', './maps')
        )

        def log_callback(level, message, log_data=None):
            log_entry = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'level': level,
                'message': message,
                'data': log_data
            }
            exploration_status['logs'].append(log_entry)

        exploration_status['logs'] = []
        explorer_instance = CoordMapBuilder(client, config, log_callback)

        exploration_status['running'] = True
        exploration_status['package'] = package_name
        exploration_status['version'] = 'coord'
        exploration_status['progress'] = {
            'pages_discovered': 0,
            'transitions_discovered': 0,
            'current_page': None
        }
        exploration_status['result'] = None

        log_callback('info', f'[v4] 鍧愭爣椹卞姩鎺㈢储: {package_name}')

        exploration_result = explorer_instance.explore(package_name)

        exploration_status['running'] = False
        exploration_status['progress'] = {
            'pages_discovered': exploration_result['page_count'],
            'transitions_discovered': exploration_result['transition_count'],
            'current_page': 'completed'
        }
        exploration_status['result'] = {
            'pages': exploration_result['page_count'],
            'transitions': exploration_result['transition_count'],
            'time': round(exploration_result['exploration_time_seconds'], 2),
            'actions': exploration_result['total_actions']
        }

        return jsonify({
            'success': True,
            'message': f'[v4] 鎺㈢储瀹屾垚: {exploration_result["page_count"]} 椤甸潰, {exploration_result["transition_count"]} 璺宠浆',
            'result': exploration_status['result']
        })

    except Exception as e:
        import traceback
        exploration_status['running'] = False
        return jsonify({
            'success': False,
            'message': f'鎺㈢储澶辫触: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


def _run_node_explore_once(package_name: str, data: dict, log_prefix: str = '', keep_running: bool = False):
    global client, explorer_instance, exploration_result, exploration_status
    builder = None
    try:
        from map_builder import NodeMapBuilder

        config = ExplorationConfig(
            max_pages=data.get('max_pages', 30),
            max_depth=data.get('max_depth', 3),
            max_time_seconds=data.get('max_time_seconds', 1800),
            action_delay_ms=data.get('action_delay_ms', 800),
            output_dir=data.get('output_dir', './maps')
        )

        def log_callback(level, message, log_data=None):
            msg = f"{log_prefix}{message}" if log_prefix else message
            _append_explore_log(level, msg, log_data)

        builder = NodeMapBuilder(client, config, log_callback)
        explore_mode = data.get('explore_mode', 'serial')
        click_delay = data.get('click_delay', 1.5)
        builder.set_mode(explore_mode)
        builder.set_click_delay(click_delay)

        with EXPLORATION_LOCK:
            explorer_instance = builder
            exploration_status['running'] = True
            exploration_status['package'] = package_name
            exploration_status['version'] = 'node'
            exploration_status['progress'] = {
                'nodes_discovered': 0,
                'nodes_explored': 0,
                'current_node': None
            }
            exploration_status['result'] = None

        log_callback('info', f'[v5] Node explore start: {package_name}')
        result = builder.explore(package_name)

        with EXPLORATION_LOCK:
            exploration_result = result
            exploration_status['progress'] = {
                'pages_discovered': result.get('total_pages', 0),
                'transitions_discovered': result.get('total_transitions', 0),
                'current_node': 'completed'
            }
            exploration_status['result'] = {
                'total_pages': result.get('total_pages', 0),
                'total_transitions': result.get('total_transitions', 0),
                'time': round(float(result.get('exploration_time_seconds', 0.0)), 2),
                'actions': result.get('total_actions', 0)
            }
        return True, builder, result, None
    except Exception as e:
        import traceback
        with EXPLORATION_LOCK:
            exploration_status['result'] = {
                'error': str(e),
            }
        return False, builder, None, {
            'message': str(e),
            'traceback': traceback.format_exc()
        }
    finally:
        if not keep_running:
            with EXPLORATION_LOCK:
                exploration_status['running'] = False


def _node_queue_worker(run_id: str, request_data: dict):
    global QUEUE_THREAD, exploration_status

    retry_max = max(1, min(5, int(request_data.get('retry_max', 2))))
    auto_save = bool(request_data.get('auto_save', True))
    output_root = str(request_data.get('output_root') or 'map_repo').strip()
    config_snapshot = _snapshot_node_config(request_data)

    _append_explore_log('info', f'[queue] run started: run_id={run_id}, retry_max={retry_max}, auto_save={auto_save}')

    while True:
        with QUEUE_LOCK:
            queue_state = exploration_status.get('queue') or _new_queue_state()
            if queue_state.get('stopping'):
                break
            pending = list(queue_state.get('pending') or [])
            if not pending:
                break
            package_name = pending.pop(0)
            attempts = dict(queue_state.get('attempts') or {})
            attempt = int(attempts.get(package_name, 0)) + 1
            attempts[package_name] = attempt
            queue_state['attempts'] = attempts
            queue_state['pending'] = pending
            queue_state['current'] = {
                'package': package_name,
                'attempt': attempt,
            }
            queue_state['retry_max'] = retry_max
            queue_state['output_root'] = output_root
            exploration_status['queue'] = queue_state

        prefix = f"[queue][pkg={package_name}][attempt={attempt}/{retry_max}] "
        ok, builder, result, err = _run_node_explore_once(
            package_name,
            request_data,
            log_prefix=prefix,
            keep_running=True
        )

        save_info = None
        if ok and auto_save and builder and getattr(builder, 'nav_map', None):
            try:
                nav_map_obj = builder.nav_map.to_dict()
                run_stats = {
                    'total_pages': result.get('total_pages', 0),
                    'total_transitions': result.get('total_transitions', 0),
                    'total_actions': result.get('total_actions', 0),
                    'exploration_time_seconds': result.get('exploration_time_seconds', 0),
                }
                save_info = _save_nav_map_to_repo(
                    package_name=package_name,
                    nav_map_obj=nav_map_obj,
                    run_stats=run_stats,
                    config_snapshot=config_snapshot,
                    source='queue_auto',
                    output_root=output_root
                )
                _append_explore_log('info', f'{prefix}saved -> {save_info["map_path_rel"]}')
            except Exception as save_err:
                ok = False
                err = {
                    'message': f'auto_save_failed: {save_err}',
                    'traceback': ''
                }

        with QUEUE_LOCK:
            queue_state = exploration_status.get('queue') or _new_queue_state()
            queue_state['current'] = None
            completed = list(queue_state.get('completed') or [])
            failed = list(queue_state.get('failed') or [])
            pending = list(queue_state.get('pending') or [])
            stopping = bool(queue_state.get('stopping'))

            if ok:
                completed.append({
                    'package': package_name,
                    'attempt': attempt,
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    'result': result or {},
                    'artifact': save_info or {},
                })
                _append_explore_log('info', f'{prefix}completed')
            else:
                reason = (err or {}).get('message') or 'unknown_error'
                if attempt < retry_max and not stopping:
                    pending.append(package_name)
                    _append_explore_log('warn', f'{prefix}failed -> moved to tail ({reason})')
                else:
                    failed.append({
                        'package': package_name,
                        'attempt': attempt,
                        'finished_at': datetime.now(timezone.utc).isoformat(),
                        'reason': reason,
                        'traceback': (err or {}).get('traceback') or '',
                    })
                    _append_explore_log('error', f'{prefix}failed permanently ({reason})')

            queue_state['pending'] = pending
            queue_state['completed'] = completed
            queue_state['failed'] = failed
            exploration_status['queue'] = queue_state

    with QUEUE_LOCK:
        queue_state = exploration_status.get('queue') or _new_queue_state()
        was_stopping = bool(queue_state.get('stopping'))
        queue_state['running'] = False
        queue_state['stopping'] = False
        queue_state['current'] = None
        queue_state['finished_at'] = datetime.now(timezone.utc).isoformat()
        exploration_status['queue'] = queue_state

    with EXPLORATION_LOCK:
        exploration_status['running'] = False
        exploration_status['package'] = None

    if was_stopping:
        _append_explore_log('warn', f'[queue] run stopped: run_id={run_id}')
    else:
        q = exploration_status.get('queue') or {}
        _append_explore_log(
            'info',
            f'[queue] run finished: run_id={run_id}, completed={len(q.get("completed") or [])}, failed={len(q.get("failed") or [])}'
        )

    with QUEUE_LOCK:
        QUEUE_THREAD = None


@app.route('/api/explore/node/queue/start', methods=['POST'])
def explore_node_queue_start():
    global QUEUE_THREAD, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'Auto Map Builder module unavailable'}), 400

    data = request.json or {}
    packages_raw = data.get('packages') or []
    if not isinstance(packages_raw, list):
        return jsonify({'success': False, 'message': 'packages must be a list'}), 400

    packages = []
    seen = set()
    for item in packages_raw:
        pkg = str(item or '').strip()
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        packages.append(pkg)

    if not packages:
        return jsonify({'success': False, 'message': 'no packages provided'}), 400

    with QUEUE_LOCK:
        alive = QUEUE_THREAD is not None and QUEUE_THREAD.is_alive()
    if exploration_status.get('running') or alive:
        return jsonify({'success': False, 'message': 'exploration is already running'}), 400

    run_id = str(uuid.uuid4())
    queue_state = _new_queue_state()
    queue_state['run_id'] = run_id
    queue_state['running'] = True
    queue_state['retry_max'] = max(1, min(5, int(data.get('retry_max', 2))))
    queue_state['pending'] = list(packages)
    queue_state['started_at'] = datetime.now(timezone.utc).isoformat()
    queue_state['output_root'] = str(data.get('output_root') or 'map_repo').strip() or 'map_repo'

    with EXPLORATION_LOCK:
        exploration_status['logs'] = []
        exploration_status['running'] = True
        exploration_status['version'] = 'node'
        exploration_status['package'] = None
        exploration_status['result'] = None
        exploration_status['queue'] = queue_state

    worker = threading.Thread(
        target=_node_queue_worker,
        args=(run_id, dict(data)),
        daemon=True
    )
    with QUEUE_LOCK:
        QUEUE_THREAD = worker
    worker.start()

    return jsonify({
        'success': True,
        'accepted': True,
        'message': f'queue accepted: {len(packages)} packages',
        'run_id': run_id,
        'queue': queue_state,
    })


@app.route('/api/explore/node/start', methods=['POST'])
def explore_node_start():
    """Start Node-driven exploration (v5)."""
    global client, explorer_instance, exploration_result, exploration_status

    error_response = _require_client_response()
    if error_response:
        return error_response

    with QUEUE_LOCK:
        queue_alive = QUEUE_THREAD is not None and QUEUE_THREAD.is_alive()
    if exploration_status.get('running') or queue_alive:
        return jsonify({'success': False, 'message': 'exploration is already running'}), 400

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'Auto Map Builder module unavailable'}), 400

    data = request.json or {}
    package_name = str(data.get('package', '')).strip()
    if not package_name:
        return jsonify({'success': False, 'message': 'package is required'}), 400

    with EXPLORATION_LOCK:
        exploration_status['logs'] = []
        exploration_status['queue'] = _new_queue_state()

    ok, _, result, err = _run_node_explore_once(package_name, data, keep_running=False)
    if not ok:
        return jsonify({
            'success': False,
            'message': f'explore failed: {(err or {}).get("message", "unknown_error")}',
            'traceback': (err or {}).get('traceback', '')
        }), 500

    return jsonify({
        'success': True,
        'message': f'[v5] exploration completed: {result.get("total_pages", 0)} pages, {result.get("total_transitions", 0)} transitions',
        'result': exploration_status.get('result') or {},
    })


@app.route('/api/maps/list', methods=['GET'])
def maps_list():
    """List saved maps from map_repo index."""
    try:
        repo_root = _resolve_map_repo_root()
        index_obj = _load_map_repo_index(repo_root)
        rows = []
        for row in list(index_obj.get('maps') or []):
            map_rel = str(row.get('map_path') or '').strip()
            if not map_rel:
                continue
            abs_map_path = _safe_path_under_root(repo_root, os.path.join(repo_root, map_rel))
            size = int(row.get('bytes') or (os.path.getsize(abs_map_path) if os.path.exists(abs_map_path) else 0))
            generated_at = str(row.get('generated_at') or '')
            map_id = str(row.get('map_id') or os.path.basename(os.path.dirname(abs_map_path)))
            rows.append({
                'package': str(row.get('package') or ''),
                'filename': f'{map_id}.json.gz',
                'filepath': abs_map_path,
                'size': size,
                'modified': generated_at,
                'map_id': map_id,
            })
        rows.sort(key=lambda x: x.get('modified') or '', reverse=True)
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/maps/latest', methods=['GET'])
def maps_latest():
    """Load latest map content from map_repo index."""
    try:
        repo_root = _resolve_map_repo_root()
        index_obj = _load_map_repo_index(repo_root)
        rows = list(index_obj.get('maps') or [])
        rows.sort(key=lambda x: str(x.get('generated_at') or ''), reverse=True)
        if not rows:
            return jsonify({'success': False, 'message': 'no map found'}), 404

        latest = rows[0]
        map_rel = str(latest.get('map_path') or '').strip()
        if not map_rel:
            return jsonify({'success': False, 'message': 'invalid latest map entry'}), 500

        abs_map_path = _safe_path_under_root(repo_root, os.path.join(repo_root, map_rel))
        if not os.path.exists(abs_map_path):
            return jsonify({'success': False, 'message': 'latest map file missing'}), 404

        content = _load_json_or_gz(abs_map_path)
        return jsonify({'success': True, 'filepath': abs_map_path, 'data': content})
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/maps/load', methods=['POST'])
def maps_load():
    """Load map JSON from map_repo filepath (.json.gz supported)."""
    try:
        data = request.json or {}
        filepath = str(data.get('filepath') or '').strip()
        if not filepath:
            return jsonify({'success': False, 'message': 'filepath is required'}), 400

        repo_root = _resolve_map_repo_root()
        candidate = filepath if os.path.isabs(filepath) else os.path.join(repo_root, filepath)
        try:
            abs_filepath = _safe_path_under_root(repo_root, candidate)
        except RuntimeError:
            return jsonify({'success': False, 'message': 'illegal path'}), 403

        if not os.path.exists(abs_filepath):
            return jsonify({'success': False, 'message': 'file not found'}), 404

        content = _load_json_or_gz(abs_filepath)
        return jsonify({'success': True, 'filepath': abs_filepath, 'data': content})
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


def _normalize_map_for_burn(raw_obj: dict, package_name: str) -> dict:
    if not isinstance(raw_obj, dict):
        raise RuntimeError('map_data must be a JSON object')

    out = dict(raw_obj)
    out['package'] = package_name

    # pages: standardize to {page_id: page_obj}
    pages_raw = out.get('pages')
    pages = {}
    if isinstance(pages_raw, dict):
        for k, v in pages_raw.items():
            page_id = str(k or '').strip()
            if not page_id:
                continue
            row = dict(v) if isinstance(v, dict) else {}
            if not row.get('page_id'):
                row['page_id'] = page_id
            pages[page_id] = row
    elif isinstance(pages_raw, list):
        for row in pages_raw:
            if not isinstance(row, dict):
                continue
            page_id = str(row.get('page_id') or row.get('id') or '').strip()
            if not page_id:
                continue
            one = dict(row)
            if not one.get('page_id'):
                one['page_id'] = page_id
            pages[page_id] = one
    out['pages'] = pages

    # transitions: standardize to list with from/to
    transitions_raw = out.get('transitions')
    transitions = []
    if isinstance(transitions_raw, list):
        for row in transitions_raw:
            if not isinstance(row, dict):
                continue
            from_page = str(row.get('from') or row.get('from_page') or '').strip()
            to_page = str(row.get('to') or row.get('to_page') or '').strip()
            if not from_page or not to_page:
                continue
            one = dict(row)
            one['from'] = from_page
            one['to'] = to_page
            transitions.append(one)
    out['transitions'] = transitions

    popups_raw = out.get('popups')
    out['popups'] = list(popups_raw) if isinstance(popups_raw, list) else []
    blocks_raw = out.get('blocks')
    out['blocks'] = list(blocks_raw) if isinstance(blocks_raw, list) else []

    return out


@app.route('/api/maps/burn', methods=['POST'])
def maps_burn():
    """Burn map from viewer to device burn lane (same map_set_gz path as command studio)."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    try:
        data = request.json or {}
        map_obj = data.get('map_data')
        if not isinstance(map_obj, dict):
            return jsonify({'success': False, 'message': 'map_data is required'}), 400

        package_name = str(data.get('package') or _extract_package_from_nav_map(map_obj)).strip()
        if not package_name:
            return jsonify({'success': False, 'message': 'package is required'}), 400

        normalized = _normalize_map_for_burn(map_obj, package_name)
        map_json_text = json.dumps(normalized, ensure_ascii=False)
        result = client.map_set_gz(package_name, map_json_text)
        ok = bool(result.get('ok'))

        return jsonify({
            'success': ok,
            'message': f'MAP_SET_GZ {"success" if ok else "failed"}: {package_name}',
            'response': result,
            'normalized': {
                'package': package_name,
                'pages': len(normalized.get('pages') or {}),
                'transitions': len(normalized.get('transitions') or []),
            }
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/map_publish/config', methods=['GET'])
def map_publish_config():
    try:
        return jsonify({
            'success': True,
            'data': {
                'default_repo': MAP_PUBLISH_DEFAULT_REPO,
                'default_base_branch': MAP_PUBLISH_DEFAULT_BASE_BRANCH,
                'default_lane': MAP_PUBLISH_DEFAULT_LANE,
                'default_publish_mode': _normalize_publish_mode(MAP_PUBLISH_DEFAULT_MODE),
                'default_maps_root': MAP_PUBLISH_DEFAULT_MAPS_ROOT,
                'token_configured': bool(MAP_PUBLISH_GITHUB_TOKEN),
                'local_map_repo_root': _resolve_map_repo_root(),
            }
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/map_publish/submit', methods=['POST'])
def map_publish_submit():
    try:
        is_multipart = (request.content_type or '').lower().startswith('multipart/form-data')
        if is_multipart:
            payload = dict(request.form or {})
            upload_file = request.files.get('map_file')
            upload_meta_file = request.files.get('meta_file')
        else:
            payload = dict(request.json or {})
            upload_file = None
            upload_meta_file = None

        source = str(payload.get('source') or '').strip().lower()
        if not source:
            source = 'upload' if upload_file is not None else 'saved'
        if source not in ('upload', 'saved'):
            return jsonify({'success': False, 'message': 'source must be upload or saved'}), 400

        publish_mode = _normalize_publish_mode(payload.get('publish_mode') or '')
        repo = _normalize_repo_full_name(payload.get('repo') or MAP_PUBLISH_DEFAULT_REPO)
        token = str(payload.get('github_token') or MAP_PUBLISH_GITHUB_TOKEN or '').strip()
        if publish_mode == 'github_api' and not token:
            return jsonify({'success': False, 'message': 'github token is required in github_api mode'}), 400

        lane = _normalize_publish_lane(payload.get('lane') or MAP_PUBLISH_DEFAULT_LANE)
        maps_root = str(payload.get('maps_root') or _lane_default_maps_root(lane)).strip().strip('/')
        if not maps_root:
            return jsonify({'success': False, 'message': 'maps_root is required'}), 400

        base_branch = str(payload.get('base_branch') or MAP_PUBLISH_DEFAULT_BASE_BRANCH).strip() or MAP_PUBLISH_DEFAULT_BASE_BRANCH
        if publish_mode == 'github_api':
            repo_info = _github_get_repo(repo, token)
            repo_default_branch = str((repo_info or {}).get('default_branch') or '').strip() or 'main'
            base_branch = str(payload.get('base_branch') or repo_default_branch or MAP_PUBLISH_DEFAULT_BASE_BRANCH).strip()

        raw_bytes = b''
        filename_hint = ''
        base_meta_obj = {}
        base_meta_hint = ''
        if source == 'upload':
            if upload_file is None:
                return jsonify({'success': False, 'message': 'map_file is required for upload source'}), 400
            raw_bytes = upload_file.read() or b''
            filename_hint = str(upload_file.filename or '')
            if not raw_bytes:
                return jsonify({'success': False, 'message': 'uploaded file is empty'}), 400
            if upload_meta_file is not None:
                meta_bytes = upload_meta_file.read() or b''
                if meta_bytes:
                    base_meta_obj = _load_meta_obj_from_bytes(meta_bytes, str(upload_meta_file.filename or ''))
                    base_meta_hint = str(upload_meta_file.filename or '')
        else:
            filepath = str(payload.get('filepath') or '').strip()
            if not filepath:
                return jsonify({'success': False, 'message': 'filepath is required for saved source'}), 400
            repo_root = _resolve_map_repo_root()
            candidate = filepath if os.path.isabs(filepath) else os.path.join(repo_root, filepath)
            try:
                abs_path = _safe_path_under_root(repo_root, candidate)
            except RuntimeError:
                return jsonify({'success': False, 'message': 'illegal filepath'}), 403
            if not os.path.exists(abs_path):
                return jsonify({'success': False, 'message': 'saved map file not found'}), 404
            with open(abs_path, 'rb') as f:
                raw_bytes = f.read()
            filename_hint = abs_path
            sidecar_meta_path = os.path.join(os.path.dirname(abs_path), 'meta.json')
            if os.path.exists(sidecar_meta_path):
                with open(sidecar_meta_path, 'rb') as f:
                    meta_bytes = f.read() or b''
                if meta_bytes:
                    base_meta_obj = _load_meta_obj_from_bytes(meta_bytes, sidecar_meta_path)
                    base_meta_hint = sidecar_meta_path

        nav_map_obj = _load_nav_map_obj_from_bytes(raw_bytes, filename_hint)
        package_name = str(base_meta_obj.get('package') or '').strip()
        if not package_name:
            package_name = _extract_package_from_nav_map(nav_map_obj)
        if not package_name:
            return jsonify({'success': False, 'message': 'package is missing in both meta.json and map content'}), 400

        package_dir = _safe_package_dir_name(package_name)
        submitted_at = datetime.now(timezone.utc).isoformat()
        stable_at = submitted_at if lane == 'stable' else ''
        nav_json_text = json.dumps(nav_map_obj, ensure_ascii=False, indent=2)
        nav_json_bytes = nav_json_text.encode('utf-8')
        nav_gz_bytes = gzip.compress(nav_json_bytes, compresslevel=6)
        sha256_hex = hashlib.sha256(nav_gz_bytes).hexdigest()
        map_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{sha256_hex[:8]}"

        map_rel_path = f"{maps_root}/{package_dir}/{map_id}/nav_map.json.gz".replace('\\', '/')
        meta_rel_path = f"{maps_root}/{package_dir}/{map_id}/meta.json".replace('\\', '/')
        description = str(base_meta_obj.get('description') or '').strip()
        source_candidate_map_id = str(base_meta_obj.get('source_candidate_map_id') or '').strip()
        generated_at = str(base_meta_obj.get('generated_at') or '').strip()

        builder_obj = base_meta_obj.get('builder')
        if not isinstance(builder_obj, dict):
            builder_obj = {}
        builder_obj = dict(builder_obj)
        if not builder_obj.get('name'):
            builder_obj['name'] = 'LXB-MapBuilder'
        builder_obj['source_file'] = filename_hint
        if base_meta_hint:
            builder_obj['source_meta_file'] = base_meta_hint

        config_obj = base_meta_obj.get('config')
        if not isinstance(config_obj, dict):
            config_obj = {}
        stats_obj = base_meta_obj.get('stats')
        if not isinstance(stats_obj, dict):
            stats_obj = {}

        meta_obj = {
            'schema_version': 'lxb.map.stable.meta.v1' if lane == 'stable' else 'lxb.map.candidate.meta.v1',
            'package': package_name,
            'map_id': map_id,
            'generated_at': generated_at,
            'submitted_at': submitted_at,
            'stable_at': stable_at,
            'lane': lane,
            'source': source,
            'description': description,
            'source_candidate_map_id': source_candidate_map_id,
            'builder': builder_obj,
            'config': config_obj,
            'stats': stats_obj,
            'artifacts': {
                'map_path': map_rel_path,
                'sha256': sha256_hex,
                'bytes_gzip': len(nav_gz_bytes),
                'bytes_json': len(nav_json_bytes),
            },
        }
        branch_seed = f"map-publish/{_safe_branch_part(package_name)}/{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sha256_hex[:8]}"
        branch_name = branch_seed
        commit_prefix = f"publish(map): {package_name} {map_id}"

        pr_title = ''
        if not pr_title:
            if lane == 'stable':
                pr_title = f"map: add stable {package_name} ({map_id})"
            else:
                pr_title = f"map: add candidate {package_name} ({map_id})"
        pr_body = (
            f"Auto-submitted by LXB-MapBuilder publish page.\n\n"
            f"- package: `{package_name}`\n"
            f"- map_id: `{map_id}`\n"
            f"- lane: `{lane}`\n"
            f"- stable_at: `{stable_at or '-'}`\n"
            f"- source: `{source}`\n"
            f"- sha256: `{sha256_hex}`\n"
            f"- map_path: `{map_rel_path}`\n"
            f"- meta_path: `{meta_rel_path}`\n\n"
            f"- source_candidate_map_id: `{source_candidate_map_id or '-'}`\n\n"
            f"description:\n{description or '(none)'}\n"
        )
        if publish_mode == 'github_api':
            meta_bytes = json.dumps(meta_obj, ensure_ascii=False, indent=2).encode('utf-8')

            base_sha = _github_get_branch_sha(repo, base_branch, token)
            create_ok = False
            for i in range(0, 4):
                try_branch = branch_seed if i == 0 else f"{branch_seed}-{i}"
                try:
                    _github_create_branch(repo, try_branch, base_sha, token)
                    branch_name = try_branch
                    create_ok = True
                    break
                except RuntimeError as e:
                    if 'github_api_http_422' in str(e):
                        continue
                    raise
            if not create_ok:
                raise RuntimeError('failed to create branch for publish')

            _github_put_file(
                repo=repo,
                path=map_rel_path,
                branch=branch_name,
                token=token,
                content_bytes=nav_gz_bytes,
                message=f"{commit_prefix} add nav_map",
                sha=None,
            )
            _github_put_file(
                repo=repo,
                path=meta_rel_path,
                branch=branch_name,
                token=token,
                content_bytes=meta_bytes,
                message=f"{commit_prefix} add meta",
                sha=None,
            )

            pr = _github_create_pr(
                repo=repo,
                title=pr_title,
                head=branch_name,
                base=base_branch,
                body=pr_body,
                token=token,
            )
            return jsonify({
                'success': True,
                'message': 'publish PR created',
                'data': {
                    'mode': publish_mode,
                    'repo': repo,
                    'base_branch': base_branch,
                    'branch': branch_name,
                    'package': package_name,
                    'map_id': map_id,
                    'lane': lane,
                    'sha256': sha256_hex,
                    'map_path': map_rel_path,
                    'meta_path': meta_rel_path,
                    'pr_number': (pr or {}).get('number'),
                    'pr_url': (pr or {}).get('html_url'),
                }
            })

        local_repo_root = _resolve_local_git_repo_root(payload.get('local_repo_path') or '')
        local_out = _publish_to_repo_via_local_git(
            local_repo_root=local_repo_root,
            base_branch=base_branch,
            branch_name=branch_name,
            map_rel_path=map_rel_path,
            meta_rel_path=meta_rel_path,
            nav_gz_bytes=nav_gz_bytes,
            meta_obj=meta_obj,
            commit_prefix=commit_prefix
        )
        resolved_repo = local_out.get('inferred_repo') or repo
        pr_url = ''
        if resolved_repo:
            pr_url = f"https://github.com/{resolved_repo}/compare/{urllib.parse.quote(base_branch, safe='')}...{urllib.parse.quote(branch_name, safe='')}?expand=1"
        return jsonify({
            'success': True,
            'message': 'publish branch pushed via local git',
            'data': {
                'mode': publish_mode,
                'repo': resolved_repo,
                'base_branch': base_branch,
                'branch': branch_name,
                'package': package_name,
                'map_id': map_id,
                'lane': lane,
                'sha256': sha256_hex,
                'map_path': map_rel_path,
                'meta_path': meta_rel_path,
                'local_repo_root': local_repo_root,
                'remote_url': local_out.get('remote_url', ''),
                'pr_url': pr_url,
                'pr_hint': 'Open pr_url to create pull request, or run gh pr create manually.',
            }
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/cortex/llm_config', methods=['GET'])
@app.route('/api/cortex/llm/config', methods=['GET'])
def cortex_llm_config_get():
    """Get Cortex Route Planner LLM config."""
    try:
        cfg = _load_cortex_llm_config()
        return jsonify({
            'success': True,
            'data': {
                'api_base_url': cfg.get('api_base_url', ''),
                'api_key': '***' if cfg.get('api_key') else '',
                'model_name': cfg.get('model_name', ''),
                'temperature': cfg.get('temperature', 0.1),
                'timeout': cfg.get('timeout', 30),
                'vision_jpeg_quality': int(cfg.get('vision_jpeg_quality', 35)),
                'node_exists_retries': cfg.get('node_exists_retries', 3),
                'node_exists_interval_sec': cfg.get('node_exists_interval_sec', 0.6),
                'touch_mode': cfg.get('touch_mode', 'shell_first'),
                'map_filepath': cfg.get('map_filepath', ''),
                'package_name': cfg.get('package_name', ''),
                'reconnect_before_run': bool(cfg.get('reconnect_before_run', True)),
                'use_llm_planner': bool(cfg.get('use_llm_planner', True)),
                'route_recovery_enabled': bool(cfg.get('route_recovery_enabled', False)),
                'max_route_restarts': int(cfg.get('max_route_restarts', 0)),
                'use_vlm_takeover': bool(cfg.get('use_vlm_takeover', False)),
                'fsm_max_turns': int(cfg.get('fsm_max_turns', 40)),
                'fsm_max_commands_per_turn': int(cfg.get('fsm_max_commands_per_turn', 1)),
                'fsm_max_vision_turns': int(cfg.get('fsm_max_vision_turns', 20)),
                'fsm_action_interval_sec': float(cfg.get('fsm_action_interval_sec', 0.8)),
                'fsm_screenshot_settle_sec': float(cfg.get('fsm_screenshot_settle_sec', 0.6)),
                'fsm_tap_bind_clickable': bool(cfg.get('fsm_tap_bind_clickable', False)),
                'fsm_tap_jitter_sigma_px': float(cfg.get('fsm_tap_jitter_sigma_px', 2.0)),
                'fsm_swipe_jitter_sigma_px': float(cfg.get('fsm_swipe_jitter_sigma_px', 4.0)),
                'fsm_swipe_duration_jitter_ratio': float(cfg.get('fsm_swipe_duration_jitter_ratio', 0.12)),
                'fsm_xml_stable_interval_sec': float(cfg.get('fsm_xml_stable_interval_sec', 0.3)),
                'fsm_xml_stable_samples': int(cfg.get('fsm_xml_stable_samples', 4)),
                'fsm_xml_stable_timeout_sec': float(cfg.get('fsm_xml_stable_timeout_sec', 4.0)),
            }
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/cortex/llm_config', methods=['POST'])
@app.route('/api/cortex/llm/config', methods=['POST'])
def cortex_llm_config_set():
    """Save Cortex Route Planner LLM config."""
    try:
        data = request.json or {}
        current = _load_cortex_llm_config()

        cfg = {
            'api_base_url': (data.get('api_base_url', current.get('api_base_url')) or '').strip(),
            'api_key': current.get('api_key', ''),
            'model_name': (data.get('model_name', current.get('model_name')) or '').strip(),
            'temperature': float(data.get('temperature', current.get('temperature', 0.1))),
            'timeout': int(data.get('timeout', current.get('timeout', 30))),
            'vision_jpeg_quality': int(data.get('vision_jpeg_quality', current.get('vision_jpeg_quality', 35))),
            'node_exists_retries': int(data.get('node_exists_retries', current.get('node_exists_retries', 3))),
            'node_exists_interval_sec': float(data.get('node_exists_interval_sec', current.get('node_exists_interval_sec', 0.6))),
            'touch_mode': (data.get('touch_mode', current.get('touch_mode', 'shell_first')) or 'shell_first'),
            'map_filepath': (data.get('map_filepath', current.get('map_filepath', '')) or '').strip(),
            'package_name': (data.get('package_name', current.get('package_name', '')) or '').strip(),
            'reconnect_before_run': bool(data.get('reconnect_before_run', current.get('reconnect_before_run', True))),
            'use_llm_planner': bool(data.get('use_llm_planner', current.get('use_llm_planner', True))),
            'route_recovery_enabled': bool(data.get('route_recovery_enabled', current.get('route_recovery_enabled', False))),
            'max_route_restarts': int(data.get('max_route_restarts', current.get('max_route_restarts', 0))),
            'use_vlm_takeover': bool(data.get('use_vlm_takeover', current.get('use_vlm_takeover', False))),
            'fsm_max_turns': int(data.get('fsm_max_turns', current.get('fsm_max_turns', 40))),
            'fsm_max_commands_per_turn': int(data.get('fsm_max_commands_per_turn', current.get('fsm_max_commands_per_turn', 1))),
            'fsm_max_vision_turns': int(data.get('fsm_max_vision_turns', current.get('fsm_max_vision_turns', 20))),
            'fsm_action_interval_sec': float(data.get('fsm_action_interval_sec', current.get('fsm_action_interval_sec', 0.8))),
            'fsm_screenshot_settle_sec': float(data.get('fsm_screenshot_settle_sec', current.get('fsm_screenshot_settle_sec', 0.6))),
            'fsm_tap_bind_clickable': bool(data.get('fsm_tap_bind_clickable', current.get('fsm_tap_bind_clickable', False))),
            'fsm_tap_jitter_sigma_px': float(data.get('fsm_tap_jitter_sigma_px', current.get('fsm_tap_jitter_sigma_px', 2.0))),
            'fsm_swipe_jitter_sigma_px': float(data.get('fsm_swipe_jitter_sigma_px', current.get('fsm_swipe_jitter_sigma_px', 4.0))),
            'fsm_swipe_duration_jitter_ratio': float(data.get('fsm_swipe_duration_jitter_ratio', current.get('fsm_swipe_duration_jitter_ratio', 0.12))),
            'fsm_xml_stable_interval_sec': float(data.get('fsm_xml_stable_interval_sec', current.get('fsm_xml_stable_interval_sec', 0.3))),
            'fsm_xml_stable_samples': int(data.get('fsm_xml_stable_samples', current.get('fsm_xml_stable_samples', 4))),
            'fsm_xml_stable_timeout_sec': float(data.get('fsm_xml_stable_timeout_sec', current.get('fsm_xml_stable_timeout_sec', 4.0))),
        }
        raw_key = data.get('api_key')
        if raw_key and raw_key != '***':
            cfg['api_key'] = raw_key.strip()

        _save_cortex_llm_config(cfg)
        return jsonify({'success': True, 'message': 'LLM config saved'})
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/cortex/llm_test', methods=['POST'])
@app.route('/api/cortex/llm/test', methods=['POST'])
def cortex_llm_test():
    """Test Cortex Route Planner LLM connectivity."""
    try:
        cfg = _load_cortex_llm_config()
        data = request.json or {}
        prompt = (data.get('prompt') or 'Return {"package_name":"com.test.app","target_page":"home"}').strip()
        complete = _build_llm_complete(cfg)
        output = complete(prompt)
        return jsonify({
            'success': True,
            'message': f'LLM test ok: {cfg.get("model_name", "")}',
            'response': output[:1200]
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': f'LLM test failed: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/cortex/route_then_act/run', methods=['POST'])
@app.route('/api/cortex/route_then_act', methods=['POST'])
def cortex_route_then_act_run():
    """Run route stage: resolve app -> target_page -> BFS -> device routing."""
    try:
        from src.cortex import RouteThenActCortex, RouteConfig, MapPromptPlanner

        data = request.json or {}
        conn = _resolve_run_connection(data, allow_mobile_auto=False)
        run_client = conn.client
        with conn.lock:
            _prepare_link_for_task(run_client, reconnect=bool(data.get('reconnect_before_run', True)))
        user_task = (data.get('user_task') or '').strip()
        if not user_task:
            return jsonify({'success': False, 'message': 'user_task is required'}), 400

        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'maps'))
        map_path = (data.get('map_filepath') or '').strip()
        manual_package = (data.get('package_name') or '').strip()

        planner_cfg = _load_cortex_llm_config()
        llm_complete = _build_llm_complete(planner_cfg) if bool(data.get('use_llm_planner', True)) else None

        app_resolution = {
            'mode': 'manual' if manual_package else 'auto',
            'selected_package': manual_package or None,
            'reason': '',
            'candidate_count': 0,
        }

        round1_app = {}
        round2_page = {}

        # Round-1: resolve package if map path not provided
        if not map_path:
            selected_package = manual_package

            if not selected_package:
                with conn.lock:
                    raw_apps = run_client.list_apps('user')
                installed_apps = _normalize_installed_apps(raw_apps)
                map_ready_apps = [a for a in installed_apps if _has_map_for_package(base_dir, a.get('package', ''))]
                app_resolution['candidate_count'] = len(map_ready_apps)

                if not map_ready_apps:
                    return jsonify({'success': False, 'message': 'no installed app with local map found'}), 404

                if llm_complete:
                    picked = _select_package_by_llm(llm_complete, user_task, map_ready_apps)
                    picked_pkg = picked.get('package_name') or ''
                    round1_app = picked
                    exists = any(a.get('package') == picked_pkg for a in map_ready_apps)
                    if exists:
                        selected_package = picked_pkg
                        app_resolution['mode'] = 'llm'
                        app_resolution['selected_package'] = picked_pkg
                        app_resolution['reason'] = picked.get('reason', '')
                    else:
                        # fallback: first candidate
                        selected_package = map_ready_apps[0].get('package')
                        app_resolution['mode'] = 'fallback_first_candidate'
                        app_resolution['selected_package'] = selected_package
                        app_resolution['reason'] = 'llm package invalid or out of candidates'
                else:
                    selected_package = map_ready_apps[0].get('package')
                    app_resolution['mode'] = 'fallback_no_llm'
                    app_resolution['selected_package'] = selected_package
                    app_resolution['reason'] = 'llm planner disabled'

            map_path = _pick_latest_map_file(base_dir, selected_package or None)

        # map path provided -> derive selected package from file path if possible
        if map_path:
            map_path = os.path.abspath(map_path)
            if not map_path.startswith(base_dir):
                return jsonify({'success': False, 'message': 'invalid map_filepath'}), 403
            if not os.path.exists(map_path):
                return jsonify({'success': False, 'message': 'map_filepath not found'}), 404

        cfg = data.get('route_config') or {}
        route_cfg = RouteConfig(
            node_exists_retries=int(cfg.get('node_exists_retries', 3)),
            node_exists_interval_sec=float(cfg.get('node_exists_interval_sec', 0.6)),
            max_route_restarts=int(cfg.get('max_route_restarts', 0)),
            use_vlm_takeover=False,
            vlm_takeover_timeout_sec=float(cfg.get('vlm_takeover_timeout_sec', 15.0)),
            route_recovery_enabled=bool(cfg.get('route_recovery_enabled', False)),
        )

        planner = None
        if llm_complete:
            if map_path:
                # Round-2: for selected app map, infer target_page
                round2_page = _select_target_page_by_llm(llm_complete, user_task, map_path)
                tp = (round2_page.get('target_page') or '').strip()
                if tp:
                    selected_pkg = app_resolution.get('selected_package') or ''
                    planner = _FixedPlanPlanner(selected_pkg, tp)
                else:
                    planner = MapPromptPlanner(llm_complete)
            else:
                planner = MapPromptPlanner(llm_complete)

        logs = []

        def log_callback(payload):
            logs.append(payload)

        engine = RouteThenActCortex(
            client=run_client,
            planner=planner,
            config=route_cfg,
            log_callback=log_callback,
        )

        with conn.lock:
            result = engine.run(
                user_task=user_task,
                map_path=map_path,
                start_page=(data.get('start_page') or None),
                package_name=selected_package or None,
            )

        plan_event = next((x for x in logs if x.get('event') == 'plan_ready'), {})
        route_steps = [
            {
                'index': x.get('step_index'),
                'from_page': x.get('from_page'),
                'to_page': x.get('to_page'),
                'trigger_node': x.get('trigger_node'),
            }
            for x in logs
            if x.get('event') == 'route_step' and x.get('result') == 'start'
        ]

        return jsonify({
            'success': result.get('status') == 'success',
            'map_path': map_path,
            'app_resolution': app_resolution,
            'llm_rounds': {
                'round1_app': round1_app,
                'round2_target_page': round2_page,
            },
            'planner_output': {
                'package_name': plan_event.get('package_name'),
                'target_page': plan_event.get('target_page'),
                'llm_model': planner_cfg.get('model_name') if planner else None,
            },
            'route_steps': route_steps,
            'result': result,
            'logs': logs,
            'connection_id': conn.connection_id,
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


def _run_cortex_fsm_logic(data: dict, log_callback, run_client):
    from src.cortex import CortexFSMEngine, FSMConfig, LLMPlanner, RouteConfig

    _prepare_link_for_task(run_client, reconnect=bool(data.get('reconnect_before_run', True)))

    user_task = (data.get('user_task') or '').strip()
    if not user_task:
        raise ValueError('user_task is required')

    base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'maps'))
    map_path = (data.get('map_filepath') or '').strip()
    manual_package = (data.get('package_name') or '').strip()
    use_llm_planner = bool(data.get('use_llm_planner', True))

    planner_cfg = _load_cortex_llm_config()
    llm_complete_json = _build_llm_complete(planner_cfg) if use_llm_planner else None
    llm_complete_fsm = _build_llm_complete_fsm(planner_cfg) if use_llm_planner else None
    llm_complete_with_image = _build_llm_complete_with_image(planner_cfg) if use_llm_planner else None
    llm_task_summary = _build_llm_task_summary(planner_cfg) if use_llm_planner else None

    app_resolution = {
        'mode': 'manual' if manual_package else 'auto',
        'selected_package': manual_package or None,
        'reason': '',
        'candidate_count': 0,
    }
    round1_app = {}

    # Apply touch mode preference for this run.
    touch_mode = str(data.get('touch_mode') or planner_cfg.get('touch_mode') or 'shell_first').strip()
    try:
        run_client.set_touch_mode(shell_first=(touch_mode != 'uiautomation_first'))
    except Exception:
        pass
    screenshot_quality = int(data.get('vision_jpeg_quality') or planner_cfg.get('vision_jpeg_quality') or 35)
    try:
        run_client.set_screenshot_quality(screenshot_quality)
    except Exception:
        pass

    app_candidates_for_fsm = []
    installed_apps = []
    try:
        installed_apps = _normalize_installed_apps(run_client.list_apps('user'))
    except Exception:
        installed_apps = []

    if not map_path:
        selected_package = manual_package
        if not selected_package:
            map_ready_apps = [a for a in installed_apps if _has_map_for_package(base_dir, a.get('package', ''))]
            app_candidates_for_fsm = installed_apps[:] if installed_apps else map_ready_apps[:]
            app_resolution['candidate_count'] = len(map_ready_apps)

            if llm_complete_json:
                try:
                    picked = _select_package_by_llm(llm_complete_json, user_task, installed_apps or map_ready_apps)
                    picked_pkg = picked.get('package_name') or ''
                    round1_app = picked
                    exists = any(a.get('package') == picked_pkg for a in (installed_apps or map_ready_apps))
                except Exception as e:
                    picked = {}
                    picked_pkg = ''
                    exists = False
                    round1_app = {'error': f'llm_timeout_or_error: {e}'}
                if exists:
                    selected_package = picked_pkg
                    app_resolution['mode'] = 'llm'
                    app_resolution['selected_package'] = picked_pkg
                    app_resolution['reason'] = picked.get('reason', '')
                    if not _has_map_for_package(base_dir, selected_package):
                        app_resolution['mode'] = 'llm_no_map'
                        app_resolution['note'] = 'no map for selected app; will use vision-only mode'
                else:
                    fallback_list = map_ready_apps or installed_apps
                    if fallback_list:
                        selected_package = fallback_list[0].get('package')
                        app_resolution['mode'] = 'fallback_first_candidate'
                        app_resolution['selected_package'] = selected_package
                        app_resolution['reason'] = 'llm timeout/invalid package, fallback to first candidate'
                    else:
                        raise RuntimeError('no installed apps found')
            else:
                fallback_list = map_ready_apps or installed_apps
                if fallback_list:
                    selected_package = fallback_list[0].get('package')
                    app_resolution['mode'] = 'fallback_no_llm'
                    app_resolution['selected_package'] = selected_package
                    app_resolution['reason'] = 'llm planner disabled'
                else:
                    raise RuntimeError('no installed apps found')
        else:
            app_candidates_for_fsm = [{'package': selected_package, 'name': _infer_app_name_from_package(selected_package)}]

        map_path = _pick_latest_map_file(base_dir, selected_package or None)
        # map_path may be None; engine handles no-map by going straight to VISION_ACT
    else:
        map_path = os.path.abspath(map_path)
        if not map_path.startswith(base_dir):
            raise RuntimeError('invalid map_filepath')
        if not os.path.exists(map_path):
            raise RuntimeError('map_filepath not found')
        if manual_package:
            app_candidates_for_fsm = [{'package': manual_package, 'name': _infer_app_name_from_package(manual_package)}]
        else:
            app_candidates_for_fsm = []

    cfg = data.get('route_config') or {}
    route_cfg = RouteConfig(
        node_exists_retries=int(cfg.get('node_exists_retries', 3)),
        node_exists_interval_sec=float(cfg.get('node_exists_interval_sec', 0.6)),
        max_route_restarts=int(cfg.get('max_route_restarts', 0)),
        use_vlm_takeover=bool(cfg.get('use_vlm_takeover', False)),
        vlm_takeover_timeout_sec=float(cfg.get('vlm_takeover_timeout_sec', 15.0)),
        route_recovery_enabled=bool(cfg.get('route_recovery_enabled', False)),
    )
    fsm_cfg_data = data.get('fsm_config') or {}
    fsm_cfg = FSMConfig(
        max_turns=int(fsm_cfg_data.get('max_turns', 30)),
        max_commands_per_turn=int(fsm_cfg_data.get('max_commands_per_turn', 1)),
        max_vision_turns=int(fsm_cfg_data.get('max_vision_turns', 20)),
        action_interval_sec=float(fsm_cfg_data.get('action_interval_sec', 0.8)),
        screenshot_settle_sec=float(fsm_cfg_data.get('screenshot_settle_sec', 0.6)),
        tap_bind_clickable=bool(fsm_cfg_data.get('tap_bind_clickable', False)),
        tap_jitter_sigma_px=float(fsm_cfg_data.get('tap_jitter_sigma_px', 2.0)),
        swipe_jitter_sigma_px=float(fsm_cfg_data.get('swipe_jitter_sigma_px', 4.0)),
        swipe_duration_jitter_ratio=float(fsm_cfg_data.get('swipe_duration_jitter_ratio', 0.12)),
        xml_stable_interval_sec=float(fsm_cfg_data.get('xml_stable_interval_sec', 0.3)),
        xml_stable_samples=int(fsm_cfg_data.get('xml_stable_samples', 4)),
        xml_stable_timeout_sec=float(fsm_cfg_data.get('xml_stable_timeout_sec', 4.0)),
    )

    llm_planner = LLMPlanner(llm_complete_fsm, llm_complete_with_image) if llm_complete_fsm else None
    selected_package = app_resolution.get('selected_package') or ''
    planner = _FSMPlannerBridge(selected_package=selected_package, llm_planner=llm_planner)

    logs = []
    def _log(payload):
        logs.append(payload)
        if log_callback:
            log_callback(payload)

    engine = CortexFSMEngine(
        client=run_client,
        planner=planner,
        route_config=route_cfg,
        fsm_config=fsm_cfg,
        log_callback=_log,
    )
    result = engine.run(
        user_task=user_task,
        map_path=map_path,
        start_page=(data.get('start_page') or None),
        package_name=selected_package or None,
        extra_context={
            'app_candidates': (installed_apps or app_candidates_for_fsm),
            'page_candidates': _build_page_candidates_from_map(map_path) if map_path else [],
        },
    )
    ok = result.get('status') == 'success'
    task_summary = ''
    try:
        if llm_task_summary:
            task_summary = llm_task_summary(user_task, result)
    except Exception:
        task_summary = ''
    if not task_summary:
        task_summary = _fallback_task_summary(user_task, result)
    result['task_summary'] = task_summary
    return {
        'success': ok,
        'message': None if ok else (result.get('reason') or f"fsm_failed@{result.get('state', 'UNKNOWN')}"),
        'task_summary': task_summary,
        'map_path': map_path,
        'app_resolution': app_resolution,
        'llm_rounds': {'round1_app': round1_app},
        'result': result,
        'logs': logs,
    }


@app.route('/api/cortex/fsm/run', methods=['POST'])
def cortex_fsm_run():
    data = request.json or {}
    try:
        conn = _resolve_run_connection(data, allow_mobile_auto=True)
        with conn.lock:
            payload = _run_cortex_fsm_logic(data, log_callback=None, run_client=conn.client)
        payload['connection_id'] = conn.connection_id
        return jsonify(payload)
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/cortex/fsm/start', methods=['POST'])
def cortex_fsm_start():
    data = request.json or {}
    try:
        conn = _resolve_run_connection(data, allow_mobile_auto=True)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    task_id = _task_create('cortex_fsm', connection_id=conn.connection_id, user_task=(data.get('user_task') or '').strip())
    with CONNECTIONS_LOCK:
        rec = CONNECTIONS.get(conn.connection_id)
        if rec:
            rec.running_tasks += 1
    _sync_connection_info()

    def _runner():
        try:
            def _log_with_cancel(e):
                if _task_is_cancel_requested(task_id):
                    raise RuntimeError('task_cancelled')
                _task_append(task_id, e)

            with conn.lock:
                payload = _run_cortex_fsm_logic(data, log_callback=_log_with_cancel, run_client=conn.client)
            payload['connection_id'] = conn.connection_id
            if _task_is_cancel_requested(task_id):
                _task_finish(task_id, False, payload, 'task_cancelled')
                return
            _task_finish(task_id, bool(payload.get('success')), payload, payload.get('message') or '')
        except Exception as e:
            _task_finish(task_id, False, {'success': False, 'message': str(e)}, str(e))
        finally:
            with CONNECTIONS_LOCK:
                rec = CONNECTIONS.get(conn.connection_id)
                if rec and rec.running_tasks > 0:
                    rec.running_tasks -= 1
            _sync_connection_info()

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id, 'connection_id': conn.connection_id})


@app.route('/api/cortex/task/<task_id>', methods=['GET'])
@app.route('/api/cortex/tasks/<task_id>', methods=['GET'])
def cortex_task_poll(task_id):
    cursor = int(request.args.get('cursor', '0'))
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({'success': False, 'message': 'task_not_found'}), 404
        events = t['events'][cursor:]
        next_cursor = cursor + len(events)
        return jsonify({
            'success': True,
            'task_id': task_id,
            'connection_id': t.get('connection_id', ''),
            'status': t.get('status', 'created'),
            'created_at': t.get('created_at'),
            'started_at': t.get('started_at'),
            'ended_at': t.get('ended_at'),
            'events': events,
            'next_cursor': next_cursor,
            'done': bool(t['done']),
            'task_success': bool(t['success']),
            'cancel_requested': bool(t.get('cancel_requested')),
            'result': t['result'] if t['done'] else None,
            'message': t['message'] if t['done'] else '',
            'log_file': t.get('log_file', ''),
        })


@app.route('/api/cortex/task/<task_id>/cancel', methods=['POST'])
@app.route('/api/cortex/tasks/<task_id>/cancel', methods=['POST'])
def cortex_task_cancel(task_id):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({'success': False, 'message': 'task_not_found'}), 404
        if t.get('done'):
            return jsonify({'success': True, 'message': 'task_already_done'})
        t['cancel_requested'] = True
    return jsonify({'success': True, 'message': 'cancel_requested', 'task_id': task_id})


@app.route('/api/tasks/list', methods=['GET'])
def tasks_list():
    connection_id = (request.args.get('connection_id') or '').strip()
    status_filter = (request.args.get('status') or '').strip().lower()
    with TASKS_LOCK:
        rows = []
        for t in TASKS.values():
            if connection_id and t.get('connection_id') != connection_id:
                continue
            st = (t.get('status') or '').lower()
            if status_filter and st != status_filter:
                continue
            rows.append({
                'task_id': t.get('task_id'),
                'type': t.get('type'),
                'connection_id': t.get('connection_id'),
                'status': t.get('status'),
                'success': bool(t.get('success')),
                'done': bool(t.get('done')),
                'message': t.get('message') or '',
                'user_task': t.get('user_task') or '',
                'created_at': t.get('created_at'),
                'started_at': t.get('started_at'),
                'ended_at': t.get('ended_at'),
                'event_count': len(t.get('events') or []),
                'log_file': t.get('log_file') or '',
            })
    rows.sort(key=lambda x: x.get('created_at') or '', reverse=True)
    running = sum(1 for x in rows if x.get('status') == 'running')
    return jsonify({'success': True, 'running': running, 'count': len(rows), 'data': rows})


@app.route('/api/tasks/<task_id>/logs', methods=['GET'])
def task_logs(task_id):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({'success': False, 'message': 'task_not_found'}), 404
        log_file = t.get('log_file') or ''
        in_memory_events = list(t.get('events') or [])
    if log_file and os.path.exists(log_file):
        events = []
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    events.append(json.loads(line))
        except Exception:
            events = in_memory_events
    else:
        events = in_memory_events
    return jsonify({'success': True, 'task_id': task_id, 'log_file': log_file, 'events': events})


def _pick_latest_map_file(base_dir: str, package_name: str = None) -> str:
    """Pick latest nav_map_*.json, optionally constrained by package."""
    import os
    import glob

    all_maps = []

    if package_name:
        pkg_dir = package_name.replace('.', '_')
        pkg_path = os.path.join(base_dir, pkg_dir)
        if os.path.isdir(pkg_path):
            pattern = os.path.join(pkg_path, 'nav_map_*.json')
            for filepath in glob.glob(pattern):
                all_maps.append((os.path.getmtime(filepath), filepath))
    else:
        if os.path.isdir(base_dir):
            for pkg_dir in os.listdir(base_dir):
                pkg_path = os.path.join(base_dir, pkg_dir)
                if not os.path.isdir(pkg_path):
                    continue
                pattern = os.path.join(pkg_path, 'nav_map_*.json')
                for filepath in glob.glob(pattern):
                    all_maps.append((os.path.getmtime(filepath), filepath))

    if not all_maps:
        return ""
    all_maps.sort(key=lambda x: x[0], reverse=True)
    return all_maps[0][1]


@app.route('/api/explore/node/save', methods=['POST'])
def explore_node_save():
    """Save current node exploration result in map-repo format."""
    global explorer_instance, exploration_status

    if not explorer_instance:
        return jsonify({'success': False, 'message': 'no exploration result'}), 400

    if exploration_status.get('version') != 'node':
        return jsonify({'success': False, 'message': 'current result is not node exploration'}), 400

    try:
        data = request.json or {}
        output_root = str(data.get('output_root') or 'map_repo').strip() or 'map_repo'
        package_name = str(data.get('package') or exploration_status.get('package') or 'unknown').strip() or 'unknown'

        nav_map_obj = None
        if getattr(explorer_instance, 'nav_map', None):
            nav_map_obj = explorer_instance.nav_map.to_dict()
        if not isinstance(nav_map_obj, dict):
            return jsonify({'success': False, 'message': 'missing nav_map data'}), 400

        run_result = exploration_status.get('result') or {}
        run_stats = {
            'total_pages': int(run_result.get('total_pages') or 0),
            'total_transitions': int(run_result.get('total_transitions') or 0),
            'total_actions': int(run_result.get('actions') or 0),
            'exploration_time_seconds': float(run_result.get('time') or 0.0),
        }

        save_info = _save_nav_map_to_repo(
            package_name=package_name,
            nav_map_obj=nav_map_obj,
            run_stats=run_stats,
            config_snapshot={},
            source='manual_save',
            output_root=output_root,
        )

        return jsonify({
            'success': True,
            'message': f'saved map-repo artifact: {save_info["map_path_rel"]}',
            'filepath': save_info['map_path'],
            'map_id': save_info['map_id'],
            'meta_path': save_info['meta_path'],
            'sha256': save_info['sha256'],
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500



@app.route('/api/explore/screenshot/<path:filename>', methods=['GET'])
def explore_screenshot(filename):
    """鑾峰彇鎺㈢储杩囩▼涓繚瀛樼殑鎴浘"""
    import os

    # 瀹夊叏妫€鏌ワ細鍙厑璁歌闂?maps 鐩綍涓嬬殑鏂囦欢
    base_dir = os.path.abspath('./maps')
    file_path = os.path.abspath(os.path.join(base_dir, filename))

    if not file_path.startswith(base_dir):
        return jsonify({'success': False, 'message': '闈炴硶璺緞'}), 403

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'message': 'request failed'}), 404

    return Response(
        open(file_path, 'rb').read(),
        mimetype='image/jpeg'
    )


@app.route('/api/explore/result/overview', methods=['GET'])
def explore_result_overview():
    """鑾峰彇鎺㈢储缁撴灉 - app_overview.json"""
    global explorer_instance, exploration_result

    if not exploration_result:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    try:
        overview = explorer_instance.generate_overview_json()
        return jsonify({
            'success': True,
            'data': overview
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/explore/result/pages', methods=['GET'])
def explore_result_pages():
    """Get all discovered pages from exploration result."""
    global exploration_result

    if not exploration_result:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    try:
        pages_summary = []
        for page_id, page in exploration_result.pages.items():
            pages_summary.append({
                'page_id': page_id,
                'activity': page.activity,
                'description': page.page_description,
                'node_count': len(page.nodes),
                'clickable_count': len(page.clickable_nodes)
            })

        return jsonify({
            'success': True,
            'data': pages_summary
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/explore/result/page/<page_id>', methods=['GET'])
def explore_result_page(page_id):
    """鑾峰彇鎸囧畾椤甸潰璇︽儏"""
    global exploration_result

    if not exploration_result:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    if page_id not in exploration_result.pages:
        return jsonify({
            'success': False,
            'message': 'message',
            'available': list(exploration_result.pages.keys())
        }), 404

    try:
        page = exploration_result.pages[page_id]
        nodes_data = []
        for node in page.nodes:
            nodes_data.append({
                'node_id': node.node_id,
                'bounds': list(node.bounds),
                'center': list(node.center),
                'class_name': node.class_name,
                'text': node.text,
                'resource_id': node.resource_id,
                'clickable': node.clickable,
                'editable': node.editable,
                'scrollable': node.scrollable,
                'vlm_label': node.vlm_label,
                'vlm_ocr_text': node.vlm_ocr_text,
                'iou_score': node.iou_score
            })

        return jsonify({
            'success': True,
            'data': {
                'page_id': page.page_id,
                'activity': page.activity,
                'package': page.package,
                'description': page.page_description,
                'structure_hash': page.structure_hash,
                'nodes': nodes_data
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/explore/save', methods=['POST'])
def explore_save():
    """Save exploration result to file."""
    global explorer_instance

    if not explorer_instance:
        return jsonify({'success': False, 'message': '娌℃湁鎺㈢储缁撴灉'}), 400

    data = request.json or {}
    output_dir = data.get('output_dir', './maps')

    try:
        explorer_instance.save(output_dir)

        return jsonify({
            'success': True,
            'message': f'宸蹭繚瀛樺埌 {output_dir}',
            'path': output_dir
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500


# =============================================================================
# VLM 璋冭瘯 API
# =============================================================================

@app.route('/api/vlm/config', methods=['GET'])
def vlm_config_get():
    """鑾峰彇 VLM 閰嶇疆"""
    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from map_builder.vlm_engine import get_config as get_vlm_config
        config = get_vlm_config()
        return jsonify({
            'success': True,
            'data': {
                'api_base_url': config.api_base_url,
                'api_key': '***' if config.api_key else '',  # 闅愯棌 API Key
                'model_name': config.model_name,
                'enable_od': config.enable_od,
                'enable_ocr': config.enable_ocr,
                'enable_caption': config.enable_caption,
                'timeout': config.timeout,
                # 骞跺彂閰嶇疆
                'concurrent_enabled': config.concurrent_enabled,
                'concurrent_requests': config.concurrent_requests,
                'occurrence_threshold': config.occurrence_threshold or 2
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/vlm/config', methods=['POST'])
def vlm_config_set():
    """璁剧疆 VLM 閰嶇疆"""
    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from map_builder.vlm_engine import VLMConfig, set_config, get_config

        data = request.json
        current_config = get_config()

        # 鍒涘缓鏂伴厤缃紝淇濈暀鏈慨鏀圭殑瀛楁
        new_config = VLMConfig(
            api_base_url=data.get('api_base_url', current_config.api_base_url),
            api_key=data.get('api_key', current_config.api_key) if data.get('api_key') != '***' else current_config.api_key,
            model_name=data.get('model_name', current_config.model_name),
            enable_od=data.get('enable_od', current_config.enable_od),
            enable_ocr=data.get('enable_ocr', current_config.enable_ocr),
            enable_caption=data.get('enable_caption', current_config.enable_caption),
            timeout=data.get('timeout', current_config.timeout),
            # 骞跺彂閰嶇疆
            concurrent_enabled=data.get('concurrent_enabled', current_config.concurrent_enabled),
            concurrent_requests=data.get('concurrent_requests', current_config.concurrent_requests),
            occurrence_threshold=data.get('occurrence_threshold', current_config.occurrence_threshold or 2)
        )

        set_config(new_config)

        return jsonify({
            'success': True,
            'message': 'message',
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/vlm/test', methods=['POST'])
def vlm_test():
    """娴嬭瘯 VLM API 杩炴帴"""
    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        from map_builder.vlm_engine import VLMConfig, VLMEngine
        import base64

        data = request.json
        api_url = data.get('api_base_url', '')
        api_key = data.get('api_key', '')
        model_name = data.get('model_name', 'qwen-vl-plus')

        if not api_url or not api_key:
            return jsonify({'success': False, 'message': '璇锋彁渚?API URL 鍜?API Key'}), 400

        # 鍒涘缓涓存椂閰嶇疆
        test_config = VLMConfig(
            api_base_url=api_url,
            api_key=api_key,
            model_name=model_name,
            timeout=30
        )

        # 鍒涘缓寮曟搸骞舵祴璇?        engine = VLMEngine(test_config)

        # 鍒涘缓涓€涓畝鍗曠殑娴嬭瘯鍥剧墖 (1x1 绾㈣壊鍍忕礌)
        from PIL import Image
        from io import BytesIO
        img = Image.new('RGB', (100, 100), color='red')
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        test_image = buffer.getvalue()

        # 璋冪敤 API
        response = engine._call_api(test_image, "Describe this image color in one short sentence.")

        return jsonify({
            'success': True,
            'message': f'API 杩炴帴鎴愬姛锛屾ā鍨? {model_name}',
            'response': response[:500] if response else ''
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': f'API 娴嬭瘯澶辫触: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@app.route('/api/debug/vlm_status', methods=['GET', 'POST'])
def debug_vlm_status():
    """Check VLM availability and runtime configuration."""
    try:
        status = {
            'available': VLM_AVAILABLE,
            'message': 'message',
        }

        if VLM_AVAILABLE:
            try:
                from map_builder.vlm_engine import get_config as get_vlm_config
                config = get_vlm_config()
                engine = VLMEngine()
                status['model_available'] = engine.is_available()
                status['model_name'] = config.model_name
                status['api_configured'] = bool(config.api_base_url and config.api_key)
            except Exception as e:
                status['model_available'] = False
                status['error'] = str(e)

        return jsonify({'success': True, 'data': status})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/debug/vlm_test', methods=['POST'])
def debug_vlm_test():
    """娴嬭瘯 VLM 鎺ㄧ悊"""
    global client

    error_response = _require_client_response()
    if error_response:
        return error_response

    if not VLM_AVAILABLE:
        return jsonify({'success': False, 'message': 'request failed'}), 400

    try:
        import time

        # 鑾峰彇鎴浘
        screenshot = client.request_screenshot()
        if not screenshot:
            return jsonify({'success': False, 'message': '鎴浘澶辫触'}), 400

        # VLM 鎺ㄧ悊
        engine = VLMEngine()
        start = time.time()
        result = engine.infer(screenshot)
        elapsed = (time.time() - start) * 1000

        # 搴忓垪鍖栫粨鏋?        detections = []
        for det in result.detections:
            detections.append({
                'label': det.label,
                'bbox': list(det.bbox),
                'ocr_text': det.ocr_text
            })

        return jsonify({
            'success': True,
            'message': f'VLM 鎺ㄧ悊鎴愬姛: {len(detections)} 涓娴? {elapsed:.0f}ms',
            'response': {
                'page_caption': result.page_caption,
                'detections': detections,
                'inference_time_ms': elapsed
            }
        })

    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500



def cmd_cortex_fsm_run():
    """Run device-side Cortex FSM: INIT -> APP_RESOLVE -> ROUTE_PLAN -> ROUTING -> VISION_ACT."""
    error_response = _require_client_response()
    if error_response:
        return error_response

    data = request.json or {}
    user_task = (data.get('user_task') or '').strip()
    package_name = (data.get('package') or '').strip()
    map_path = (data.get('map_path') or '').strip()
    start_page = (data.get('start_page') or '').strip()

    if not user_task:
        return jsonify({'success': False, 'message': 'user_task is required'}), 400

    try:
        result = client.cortex_fsm_run(
            user_task=user_task,
            package=package_name or None,
            map_path=map_path or None,
            start_page=start_page or None,
        )
        ok = bool(result.get('ok')) and result.get('status') == 'submitted'
        task_id = (result.get('task_id') or '').strip()
        if ok:
            msg = f'CORTEX_FSM_RUN 宸叉彁浜? task_id={task_id or "<unknown>"}'
        else:
            msg = f'CORTEX_FSM_RUN 鎻愪氦澶辫触: {result}'
        return jsonify({
            'success': ok,
            'message': msg,
            'response': result,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


if __name__ == '__main__':
    print("=" * 60)
    print("LXB Web Console")
    print("=" * 60)
    print("璁块棶鍦板潃: http://localhost:5000")
    if VLM_AVAILABLE:
        print("VLM 妯″潡: 鍙敤")
    else:
        print("VLM 妯″潡: 涓嶅彲鐢?(闇€瑕佸畨瑁?torch, transformers)")
    print("鎸?Ctrl+C 鍋滄鏈嶅姟")
    print("=" * 60)

    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)




