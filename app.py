import os, json, re, time, asyncio, threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app, origins="*")

OG_OK = False
llm_client = None
og = None
WORKING_MODEL = None

# Models ordered cheapest/fastest first — Claude Haiku is cheapest on testnet
MODEL_PRIORITY = [
    "CLAUDE_HAIKU_4_5",
    "CLAUDE_SONNET_4_5",
    "CLAUDE_SONNET_4_6",
    "GPT_5_MINI",
    "GEMINI_2_5_FLASH_LITE",
    "GEMINI_2_5_FLASH",
]

_loop = None

def _start_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def _run(coro, timeout=120):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)

# Start event loop thread immediately
threading.Thread(target=_start_loop, daemon=True).start()
time.sleep(0.3)

try:
    import opengradient as _og
    import ssl, urllib3

    og = _og
    ssl._create_default_https_context = ssl._create_unverified_context
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    private_key = os.environ.get("OG_PRIVATE_KEY", "")
    if not private_key:
        raise ValueError("OG_PRIVATE_KEY not set")

    llm_client = og.LLM(private_key=private_key)

    try:
        approval = llm_client.ensure_opg_approval(min_allowance=0.5)
        print(f"OPG approval: {approval}")
    except Exception as e:
        print(f"Approval warning (non-fatal): {e}")

    OG_OK = True
    print("OG connected")
except Exception as e:
    print(f"Demo mode: {e}")


def self_ping():
    import urllib.request
    time.sleep(60)
    while True:
        time.sleep(240)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
            urllib.request.urlopen(f"{url}/health", timeout=10)
            print("Self-ping OK")
        except Exception as e:
            print(f"Self-ping failed: {e}")


def extract_raw(result):
    """Extract text from OG result. Per docs: result.chat_output['content']"""
    if not result:
        return ""
    # Primary path per OG docs
    co = getattr(result, 'chat_output', None)
    if co:
        if isinstance(co, dict) and co.get('content'):
            return str(co['content'])
        if isinstance(co, str) and co.strip():
            return co
    # Fallback: completion_output
    comp = getattr(result, 'completion_output', None)
    if comp and str(comp).strip():
        return str(comp)
    # Last resort: scan all string attrs
    for attr in dir(result):
        if attr.startswith('_'):
            continue
        try:
            val = getattr(result, attr)
            if callable(val):
                continue
            if isinstance(val, str) and ('"risk_score"' in val or '<JSON>' in val):
                return val
        except:
            pass
    return ""


def probe_models():
    """Find a working model. Runs in background — never blocks gunicorn."""
    global WORKING_MODEL
    if not OG_OK or llm_client is None:
        return
    print("Probing models...")
    for name in MODEL_PRIORITY:
        if not hasattr(og.TEE_LLM, name):
            print(f"  SKIP {name} — not in SDK")
            continue
        model = getattr(og.TEE_LLM, name)
        try:
            print(f"Testing {name}...")
            result = _run(llm_client.chat(
                model=model,
                messages=[{"role": "user", "content": "Say: OK"}],
                max_tokens=10,
                temperature=0.0,
            ), timeout=30)
            raw = extract_raw(result)
            print(f"  {name} raw: {repr(raw[:80])}")
            if raw and raw.strip():
                WORKING_MODEL = model
                print(f"✓ Using model: {name}")
                return
        except Exception as e:
            print(f"  FAIL {name}: {e}")
    print("No working model found.")


def parse_json(raw):
    if not raw or not raw.strip():
        return {"error": "Empty response"}
    m = re.search(r"<JSON>(.*?)</JSON>", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception as e:
            print(f"JSON parse error inside <JSON> tags: {e}")
    m = re.search(r'\{[\s\S]*?"risk_score"[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except:
            pass
    return {"error": "Parse failed", "raw": raw[:300]}


def call_llm(messages, retries=3):
    global WORKING_MODEL
    if not OG_OK or llm_client is None:
        return {"error": "OpenGradient not available"}

    if WORKING_MODEL is None:
        probe_models()
    if WORKING_MODEL is None:
        return {"error": "No working LLM model found — check OG testnet status"}

    last_error = ""
    for attempt in range(retries):
        try:
            print(f"LLM attempt {attempt+1} | model: {WORKING_MODEL}")
            result = _run(llm_client.chat(
                model=WORKING_MODEL,
                messages=messages,
                max_tokens=3000,
                temperature=0.3,
            ), timeout=110)
            raw = extract_raw(result)
            print(f"Raw response (first 200): {repr(raw[:200])}")
            if not raw.strip():
                last_error = "Empty response from model"
                time.sleep(2)
                continue
            parsed = parse_json(raw)
            if "error" in parsed:
                last_error = parsed["error"]
                time.sleep(1)
                continue
            tx = getattr(result, "transaction_hash", None) or getattr(result, "payment_hash", None)
            if tx:
                parsed["proof"] = {
                    "transaction_hash": tx,
                    "explorer_url": f"https://explorer.opengradient.ai/tx/{tx}",
                }
            return parsed
        except Exception as e:
            last_error = str(e)
            print(f"LLM error attempt {attempt+1}: {e}")
            if "402" in str(e):
                WORKING_MODEL = None
                probe_models()
                if WORKING_MODEL is None:
                    break
            else:
                time.sleep(3)

    return {"error": f"All attempts failed: {last_error}"}


SYSTEM_PROMPT = """You are an expert legal analyst. Analyze the provided legal document and reply ONLY with valid JSON inside <JSON>...</JSON> tags. No text outside the tags.

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
      "explanation": "Clear explanation of why this clause is risky.",
      "quote": "Exact or paraphrased text from the clause",
      "recommendation": "Specific actionable advice."
    }
  ],
  "recommendations": [
    "Consult a licensed attorney before signing",
    "Request removal of the non-compete clause"
  ]
}
</JSON>

Rules:
- risk_score: 0-100 (higher = more risky for the signing party)
- risks: 3-8 issues ordered high to medium to low
- level: must be exactly "high", "medium", or "low"
- Be specific, not generic
"""


@app.route("/")
def index():
    return send_from_directory('.', 'index.html')


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "og": OG_OK,
        "model": str(WORKING_MODEL) if WORKING_MODEL else None,
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    doc_text = (data.get("doc_text") or "").strip()
    pdf_base64 = data.get("pdf_base64")
    doc_type = (data.get("doc_type") or "Legal Document").strip()

    if not doc_text and not pdf_base64:
        return jsonify({"error": "doc_text or pdf_base64 is required"}), 400

    print(f"\nAnalyzing | type: '{doc_type}' | chars: {len(doc_text)}")

    user_content = []

    if pdf_base64:
        user_content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_base64}
        })

    if doc_text:
        user_content.append({"type": "text", "text": f"LEGAL DOCUMENT:\n\n{doc_text}"})

    user_content.append({"type": "text", "text": f"Document type: {doc_type}\n\nAnalyze and return the JSON."})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content if len(user_content) > 1 else user_content[0]["text"]}
    ]

    result = call_llm(messages)
    return jsonify(result)


def _startup():
    threading.Thread(target=self_ping, daemon=True).start()
    def _delayed_probe():
        time.sleep(8)
        probe_models()
    threading.Thread(target=_delayed_probe, daemon=True).start()


def post_fork(server, worker):
    _startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"LexGuard on :{port} | OG: {'live' if OG_OK else 'demo'}")
    _startup()
    app.run(host="0.0.0.0", port=port, debug=False)
