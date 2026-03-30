

import os
import json
import re
import time
import asyncio
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app)

# ============================================================
# OpenGradient Setup (как в MedAI, но без approve)
# ============================================================
OG_OK = False
llm_client = None
og = None
WORKING_MODEL = None

MODEL_PRIORITY = [
    "GEMINI_2_5_FLASH_LITE",
    "GEMINI_2_5_FLASH",
    "CLAUDE_HAIKU_4_5",
    "GPT_5_MINI",
]

_loop = None
_loop_thread = None

def _start_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def _run(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=90)

try:
    import opengradient as _og
    import ssl
    import urllib3

    og = _og
    ssl._create_default_https_context = ssl._create_unverified_context
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    _loop_thread = threading.Thread(target=_start_loop, daemon=True)
    _loop_thread.start()
    time.sleep(0.2)

    private_key = os.environ.get("OG_PRIVATE_KEY", "")
    if private_key:
        llm_client = og.LLM(private_key=private_key)
        OG_OK = True
        print("✅ OpenGradient connected")
    else:
        print("⚠️ No OG_PRIVATE_KEY in env")

except Exception as e:
    print(f"❌ OpenGradient init error: {e}")
    OG_OK = False

def probe_models():
    global WORKING_MODEL
    if not OG_OK or llm_client is None or og is None:
        return False

    print("🔍 Probing models...")
    for name in MODEL_PRIORITY:
        if not hasattr(og.TEE_LLM, name):
            continue
        model_enum = getattr(og.TEE_LLM, name)
        try:
            print(f"Testing {name}...")
            result = _run(llm_client.chat(
                model=model_enum,
                messages=[{"role": "user", "content": "Reply: OK"}],
                max_tokens=5,
                temperature=0.0,
                # КЛЮЧЕВОЙ ПАРАМЕТР: используем PRIVATE settlement mode
                x402_settlement_mode=og.x402SettlementMode.PRIVATE
            ))
            raw = extract_raw(result)
            if raw and raw.strip():
                WORKING_MODEL = model_enum
                print(f"✅ Using model: {name}")
                return True
        except Exception as e:
            print(f"  ❌ {name}: {e}")
    print("❌ No working model found")
    return False

# ============================================================
# System Prompt
# ============================================================
SYSTEM_PROMPT = """You are an expert legal analyst. Analyze the provided legal document and reply ONLY with valid JSON inside <JSON>...</JSON> tags. No text outside.

Return this exact structure:
<JSON>
{
  "document_type": "Employment Agreement",
  "risk_score": 62,
  "clause_count": 24,
  "summary": "2-3 sentence summary of the document and overall risk assessment.",
  "risks": [
    {
      "level": "high",
      "title": "Overly broad non-compete clause",
      "clause": "Section 8.2",
      "explanation": "Clear explanation of why this clause is risky",
      "quote": "Exact text from the problematic clause",
      "recommendation": "Specific actionable advice"
    }
  ],
  "recommendations": [
    "Recommendation 1",
    "Recommendation 2"
  ]
}
</JSON>

Rules:
- risk_score: 0-100 (higher = more risky for the person signing)
- risks: 3-8 specific issues, ordered high → medium → low
- Each risk MUST have: level, title, explanation, recommendation
- Be specific and focus on clauses that could harm the person signing
"""

# ============================================================
# Helper functions
# ============================================================
def extract_raw(result):
    if not result:
        return ""
    co = getattr(result, 'chat_output', None)
    if co:
        if isinstance(co, dict):
            for k in ('content', 'text', 'message', 'response', 'output'):
                if co.get(k):
                    return str(co[k])
        elif isinstance(co, str) and co.strip():
            return co
    comp = getattr(result, 'completion_output', None)
    if comp and str(comp).strip():
        return str(comp)
    if hasattr(result, 'text'):
        return result.text
    return ""

def parse_json(raw):
    if not raw or not raw.strip():
        return {"error": "Empty response"}
    
    m = re.search(r"<JSON>(.*?)</JSON>", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception as e:
            print(f"JSON parse error: {e}")
    
    m = re.search(r'\{[\s\S]*?"risk_score"[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except:
            pass
    
    return {"error": "Parse failed", "raw": raw[:300]}

def call_llm(messages):
    """Вызов LLM через OpenGradient Python SDK"""
    global WORKING_MODEL
    
    if not OG_OK or llm_client is None:
        return {"error": "OpenGradient not available"}
    
    if WORKING_MODEL is None:
        if not probe_models():
            return {"error": "No working model found"}
    
    for attempt in range(3):
        try:
            print(f"🔄 LLM attempt {attempt+1} | model: {WORKING_MODEL}")
            
            result = _run(llm_client.chat(
                model=WORKING_MODEL,
                messages=messages,
                max_tokens=3000,
                temperature=0.3,
                # КЛЮЧЕВОЙ ПАРАМЕТР: не требует on-chain записей
                x402_settlement_mode=og.x402SettlementMode.PRIVATE
            ))
            
            raw = extract_raw(result)
            print(f"Response length: {len(raw)} chars")
            
            if raw and raw.strip():
                parsed = parse_json(raw)
                if "error" not in parsed:
                    # Добавляем proof (опционально)
                    tx = getattr(result, "transaction_hash", None)
                    if tx:
                        parsed["proof"] = {
                            "transaction_hash": tx,
                            "explorer_url": f"https://explorer.opengradient.ai/tx/{tx}"
                        }
                    return parsed
                else:
                    print(f"Parse error: {parsed['error']}")
            else:
                print("Empty response")
                
        except Exception as e:
            print(f"❌ LLM error attempt {attempt+1}: {e}")
            if "402" in str(e) or "insufficient" in str(e).lower():
                print("⚠️ Need OPG tokens! Get from faucet.opengradient.ai")
            time.sleep(2)
    
    return {"error": "All attempts failed"}

# ============================================================
# Routes
# ============================================================
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "og_connected": OG_OK,
        "model_ready": WORKING_MODEL is not None,
        "model": str(WORKING_MODEL) if WORKING_MODEL else None
    })

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    doc_text = data.get("doc_text", "").strip()
    pdf_base64 = data.get("pdf_base64")
    doc_type = data.get("doc_type", "").strip()
    
    if not doc_text and not pdf_base64:
        return jsonify({"error": "doc_text or pdf_base64 is required"}), 400
    
    print(f"\n📄 Analyzing: {doc_type} ({len(doc_text)} chars)")
    
    # Формируем сообщение
    if doc_text:
        if len(doc_text) > 8000:
            doc_text = doc_text[:8000] + "\n...[truncated]"
        user_content = f"Document type: {doc_type}\n\nLegal document text:\n\n{doc_text}"
    else:
        user_content = f"Document type: {doc_type}\n\nPDF document uploaded."
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]
    
    result = call_llm(messages)
    
    # Если есть ошибка, возвращаем её
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    
    return jsonify(result)

@app.route("/probe", methods=["GET"])
def probe():
    """Принудительный поиск модели"""
    success = probe_models()
    return jsonify({
        "success": success,
        "model": str(WORKING_MODEL) if WORKING_MODEL else None
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    print(f"\n🚀 LexGuard Backend on :{port}")
    print(f"🔑 OG_PRIVATE_KEY: {'✅ set' if os.environ.get('OG_PRIVATE_KEY') else '❌ missing'}")
    
    if OG_OK:
        print("🔄 Probing models...")
        probe_models()
    
    app.run(host="0.0.0.0", port=port, debug=False)
