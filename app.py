import os, json, re, time, asyncio, threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app)

OG_OK = False
llm_client = None
og = None
WORKING_MODEL = None

# Все возможные модели
MODEL_PRIORITY = [
    "GPT_5_MINI",
    "CLAUDE_HAIKU_4_5",
    "GEMINI_2_5_FLASH",
    "GEMINI_2_5_FLASH_LITE",
    "CLAUDE_SONNET_4_5",
    "GPT_5",
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
    import ssl, urllib3
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
except Exception as e:
    print(f"❌ OpenGradient init error: {e}")

def probe_models():
    global WORKING_MODEL
    if not OG_OK or llm_client is None or og is None:
        return False
    
    print("🔍 Probing models...")
    for name in MODEL_PRIORITY:
        if not hasattr(og.TEE_LLM, name):
            continue
        model = getattr(og.TEE_LLM, name)
        try:
            print(f"Testing {name}...")
            result = _run(llm_client.chat(
                model=model,
                messages=[{"role": "user", "content": "Reply: OK"}],
                max_tokens=5,
                temperature=0.0,
                x402_settlement_mode=og.x402SettlementMode.PRIVATE
            ))
            raw = extract_raw(result)
            if raw and raw.strip():
                WORKING_MODEL = model
                print(f"✅ Using model: {name}")
                return True
        except Exception as e:
            print(f"  ❌ {name}: {e}")
    print("❌ No working model found")
    return False

SYSTEM_PROMPT = """You are an expert legal analyst. Analyze the document and reply ONLY with valid JSON.

Return structure:
{
  "document_type": "Employment Agreement",
  "risk_score": 62,
  "clause_count": 24,
  "summary": "Brief summary",
  "risks": [
    {"level": "high", "title": "...", "explanation": "...", "recommendation": "..."}
  ],
  "recommendations": ["Rec 1", "Rec 2"]
}
"""

def extract_raw(result):
    if not result:
        return ""
    co = getattr(result, 'chat_output', None)
    if co:
        if isinstance(co, dict):
            for k in ('content', 'text', 'message', 'response'):
                if co.get(k):
                    return str(co[k])
        elif isinstance(co, str):
            return co
    return ""

def parse_json(raw):
    if not raw:
        return {"error": "Empty"}
    m = re.search(r"<JSON>(.*?)</JSON>", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except:
            pass
    m = re.search(r'\{[\s\S]*?"risk_score"[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except:
            pass
    return {"error": "Parse failed"}

def call_llm(messages):
    global WORKING_MODEL
    if not OG_OK or llm_client is None:
        return {"error": "OpenGradient not available"}
    
    if WORKING_MODEL is None:
        if not probe_models():
            return {"error": "No working model found"}
    
    for attempt in range(3):
        try:
            print(f"🔄 Attempt {attempt+1}")
            result = _run(llm_client.chat(
                model=WORKING_MODEL,
                messages=messages,
                max_tokens=2000,
                temperature=0.3,
                x402_settlement_mode=og.x402SettlementMode.PRIVATE
            ))
            raw = extract_raw(result)
            if raw:
                parsed = parse_json(raw)
                if "error" not in parsed:
                    tx = getattr(result, "transaction_hash", None)
                    if tx:
                        parsed["proof"] = {"transaction_hash": tx}
                    return parsed
        except Exception as e:
            print(f"❌ Error: {e}")
            if "402" in str(e):
                print("Need OPG tokens!")
            time.sleep(2)
    
    return {"error": "Failed after 3 attempts"}

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
    doc_type = data.get("doc_type", "Legal Document")
    
    if not doc_text:
        return jsonify({"error": "doc_text required"}), 400
    
    if len(doc_text) > 6000:
        doc_text = doc_text[:6000] + "\n...[truncated]"
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Document type: {doc_type}\n\n{doc_text}"}
    ]
    
    result = call_llm(messages)
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
