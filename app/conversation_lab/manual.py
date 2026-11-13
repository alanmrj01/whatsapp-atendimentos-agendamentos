from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.conversation_lab.adapters import DryRunBookingPort, LabConversationRepository, inbound
from app.conversations.engine import ConversationEngine
from app.conversations.outbound import OutboundMessage

SESSION_TTL_SECONDS = 2 * 60 * 60
MAX_SESSIONS = 50
MAX_HISTORY_ITEMS = 300

if os.getenv("CONVERSATION_LAB_ALLOW_EXTERNAL_IO", "false").lower() == "true":
    raise RuntimeError("conversation_lab_external_io_must_remain_disabled")


class ManualInput(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    action: str | None = Field(default=None, max_length=500)
    message_type: str = Field(default="text", pattern="^(text|image|video|audio)$")


@dataclass(slots=True)
class ManualSession:
    repository: LabConversationRepository
    port: DryRunBookingPort
    sequence: int = 1
    history: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)


_SESSIONS: dict[str, ManualSession] = {}


def _cleanup_sessions() -> None:
    now = time.monotonic()
    expired = [
        session_id
        for session_id, session in _SESSIONS.items()
        if now - session.updated_at > SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        _SESSIONS.pop(session_id, None)

    if len(_SESSIONS) <= MAX_SESSIONS:
        return
    oldest = sorted(
        _SESSIONS.items(),
        key=lambda item: item[1].updated_at,
    )
    for session_id, _ in oldest[: len(_SESSIONS) - MAX_SESSIONS]:
        _SESSIONS.pop(session_id, None)


def _session_or_404(session_id: str) -> ManualSession:
    _cleanup_sessions()
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sessão expirada ou inexistente")
    session.updated_at = time.monotonic()
    return session


def _message_options(message: OutboundMessage) -> list[dict[str, str]]:
    payload = message.outbound_payload or {}
    options: list[dict[str, str]] = []

    buttons = payload.get("buttons")
    if isinstance(buttons, list):
        for button in buttons:
            if not isinstance(button, dict):
                continue
            option_id = button.get("id")
            title = button.get("title")
            if isinstance(option_id, str) and isinstance(title, str):
                options.append({"id": option_id, "title": title})

    sections = payload.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            rows = section.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                option_id = row.get("id")
                title = row.get("title")
                if isinstance(option_id, str) and isinstance(title, str):
                    options.append({"id": option_id, "title": title})
    return options


def _serialize_outbound(message: OutboundMessage) -> dict[str, Any]:
    return {
        "role": "assistant",
        "message_type": message.message_type,
        "body": message.body or "",
        "interactive_id": message.interactive_id,
        "options": _message_options(message),
    }


def _snapshot(session_id: str, session: ManualSession) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "session_id": session_id,
            "state": session.repository.state,
            "context": session.repository.context,
            "automation_enabled": session.repository.automation_enabled,
            "handoff_status": session.repository.handoff_status,
            "customer_name": session.repository.customer_name,
            "dry_run_calls": session.port.calls,
            "history": session.history[-MAX_HISTORY_ITEMS:],
            "lab": {
                "external_io": False,
                "database": False,
                "meta_whatsapp": False,
                "billing": False,
                "tracking": False,
                "booking_actions": "dry-run",
            },
        }
    )


app = FastAPI(
    title="Alovia Manual Conversation Lab",
    version="1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "manual-conversation-lab"}


@app.post("/api/session")
async def create_session() -> dict[str, Any]:
    _cleanup_sessions()
    session_id = secrets.token_urlsafe(18)
    session = ManualSession(
        repository=LabConversationRepository(),
        port=DryRunBookingPort(),
    )
    _SESSIONS[session_id] = session
    return _snapshot(session_id, session)


@app.get("/api/session/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    session = _session_or_404(session_id)
    return _snapshot(session_id, session)


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    _SESSIONS.pop(session_id, None)
    return {"status": "deleted"}


@app.post("/api/session/{session_id}/message")
async def send_message(session_id: str, payload: ManualInput) -> dict[str, Any]:
    session = _session_or_404(session_id)

    text = " ".join((payload.text or "").split()).strip() or None
    action = " ".join((payload.action or "").split()).strip() or None
    if payload.message_type == "text" and text is None and action is None:
        raise HTTPException(status_code=422, detail="Informe texto ou ação")

    if payload.message_type != "text" and action is not None:
        raise HTTPException(status_code=422, detail="Mídia sintética não usa ação")

    if payload.message_type == "text":
        event = inbound(session.sequence, body=text, action=action)
        user_label = text or action or ""
    else:
        event = replace(
            inbound(session.sequence, body=None),
            message_type=payload.message_type,
        )
        user_label = {
            "image": "[foto sintética]",
            "video": "[vídeo sintético]",
            "audio": "[áudio sintético]",
        }[payload.message_type]

    session.history.append(
        {
            "role": "user",
            "message_type": payload.message_type,
            "body": user_label,
            "action": action,
        }
    )

    before = len(session.repository.outbounds)
    await ConversationEngine(session.repository, session.port).process(event)
    session.sequence += 1

    for stored in session.repository.outbounds[before:]:
        transition = stored.transition
        session.history.append(_serialize_outbound(transition.outbound))
        for follow_up in transition.follow_ups:
            session.history.append(_serialize_outbound(follow_up))

    session.history = session.history[-MAX_HISTORY_ITEMS:]
    session.updated_at = time.monotonic()
    return _snapshot(session_id, session)


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alovia — Laboratório Manual</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f7fb;color:#10213b}
header{background:#0b63f6;color:white;padding:14px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px}
header strong{font-size:18px}.badge{background:#fff3cd;color:#755b00;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:700}
main{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:16px;max-width:1200px;margin:16px auto;padding:0 16px}
.panel{background:white;border:1px solid #dce4ef;border-radius:14px;box-shadow:0 4px 16px rgba(20,45,80,.06)}
.chat{height:72vh;display:flex;flex-direction:column;overflow:hidden}
.notice{padding:10px 14px;background:#eaf2ff;border-bottom:1px solid #dce4ef;font-size:13px}
.messages{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:82%;padding:10px 12px;border-radius:12px;white-space:pre-wrap;line-height:1.35}
.user{align-self:flex-end;background:#d9fdd3}.assistant{align-self:flex-start;background:#eef3f8}
.options{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}.options button{border:1px solid #0b63f6;background:white;color:#0b63f6;border-radius:9px;padding:7px 9px;cursor:pointer}
.composer{border-top:1px solid #dce4ef;padding:10px;display:flex;gap:8px}.composer input{flex:1;border:1px solid #cbd5e1;border-radius:10px;padding:10px;font-size:15px}
button.primary{border:0;background:#0b63f6;color:white;border-radius:10px;padding:10px 14px;font-weight:700;cursor:pointer}
.toolbar{padding:8px 10px;display:flex;gap:8px;border-top:1px solid #eef2f7}.toolbar button,.side button{border:1px solid #cbd5e1;background:white;border-radius:9px;padding:7px 9px;cursor:pointer}
.side{padding:14px}.side h3{margin:0 0 10px}.kv{display:grid;grid-template-columns:120px 1fr;gap:6px;font-size:13px;margin-bottom:12px}.ok{color:#087a35;font-weight:700}.warn{color:#a33300;font-weight:700}
pre{background:#0f172a;color:#dbeafe;border-radius:10px;padding:10px;overflow:auto;max-height:34vh;font-size:11px}
.small{font-size:12px;color:#64748b;line-height:1.45}.danger{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:9px;margin-bottom:12px;font-size:12px}
@media(max-width:900px){main{grid-template-columns:1fr}.chat{height:68vh}}
</style>
</head>
<body>
<header><strong>ALOVIA — Laboratório Manual do Atendimento</strong><span class="badge">SEM WHATSAPP / SEM PRODUÇÃO REAL</span></header>
<main>
<section class="panel chat">
<div class="notice">Converse como um cliente real: gírias, erros, perguntas no meio do fluxo, correções e mudanças de ideia.</div>
<div id="messages" class="messages"></div>
<div class="toolbar">
<button onclick="sendMedia('image')">📷 Foto sintética</button>
<button onclick="sendMedia('video')">🎥 Vídeo sintético</button>
<button onclick="sendMedia('audio')">🎙️ Áudio sintético</button>
<button onclick="resetSession()">Nova conversa</button>
</div>
<div class="composer"><input id="input" autocomplete="off" placeholder="Digite como um cliente..." onkeydown="if(event.key==='Enter')sendText()"><button class="primary" onclick="sendText()">Enviar</button></div>
</section>
<aside class="panel side">
<div class="danger"><strong>Isolamento:</strong> repositório em memória, agenda dry-run, sem banco, Meta, billing ou tracking.</div>
<h3>Diagnóstico da sessão</h3>
<div class="kv">
<div>Estado</div><div id="state">—</div>
<div>Automação</div><div id="automation">—</div>
<div>Handoff</div><div id="handoff">—</div>
<div>Chamadas</div><div id="calls">—</div>
</div>
<div class="small">Valores do laboratório são sintéticos. O que estamos validando aqui é o comportamento do motor, cálculos configurados no adapter e a retomada correta do fluxo.</div>
<h3 style="margin-top:14px">Contexto coletado</h3>
<pre id="context">{}</pre>
</aside>
</main>
<script>
let sessionId=null;
async function api(path,options={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...options});if(!r.ok){throw new Error(await r.text())}return r.json()}
async function start(){const d=await api('/api/session',{method:'POST'});sessionId=d.session_id;render(d)}
async function resetSession(){if(sessionId)await fetch('/api/session/'+sessionId,{method:'DELETE'});await start()}
async function sendText(){const input=document.getElementById('input');const text=input.value.trim();if(!text)return;input.value='';await send({text,message_type:'text'})}
async function sendAction(id,title){await send({text:title,action:id,message_type:'text'})}
async function sendMedia(type){await send({message_type:type})}
async function send(payload){try{const d=await api('/api/session/'+sessionId+'/message',{method:'POST',body:JSON.stringify(payload)});render(d)}catch(e){alert('Falha no laboratório: '+e.message)}}
function render(d){
 const box=document.getElementById('messages');box.innerHTML='';
 for(const item of d.history||[]){
   const wrap=document.createElement('div');wrap.className='msg '+(item.role==='user'?'user':'assistant');
   const body=document.createElement('div');body.textContent=item.body||'';wrap.appendChild(body);
   if(item.role==='assistant' && item.options && item.options.length){
     const opts=document.createElement('div');opts.className='options';
     for(const op of item.options){const b=document.createElement('button');b.textContent=op.title;b.onclick=()=>sendAction(op.id,op.title);opts.appendChild(b)}
     wrap.appendChild(opts);
   }
   box.appendChild(wrap);
 }
 box.scrollTop=box.scrollHeight;
 document.getElementById('state').textContent=d.state||'—';
 document.getElementById('automation').innerHTML=d.automation_enabled?'<span class="ok">ativa</span>':'<span class="warn">desativada</span>';
 document.getElementById('handoff').innerHTML=d.handoff_status==='none'?'<span class="ok">nenhum</span>':'<span class="warn">'+d.handoff_status+'</span>';
 document.getElementById('calls').textContent=(d.dry_run_calls||[]).join(', ')||'—';
 document.getElementById('context').textContent=JSON.stringify(d.context||{},null,2);
}
start();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        HTML,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
