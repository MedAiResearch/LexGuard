import os, json, re, time, asyncio, threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app, origins="*")

OG_OK = False
llm_client = None
og = None
WORKING_MODEL = None

MODEL_PRIORITY = [
    "GEMINI_2_5_FLASH_LITE",
    "GEMINI_2_5_FLASH",
    "CLAUDE_HAIKU_4_5",
    "GPT_5_MINI",
    "CLAUDE_SONNET_4_5",
    "CLAUDE_SONNET_4_6",
    "GEMINI_2_5_PRO",
    "CLAUDE_OPUS_4_5",
    "GPT_5",
    "O4_MINI",
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
    return future.result(timeout=120)

try:
    import opengradient as _og
    import ssl, urllib3

    og = _og
    ssl._create_default_https_context = ssl._create_unverified_context
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    _loop_thread = threading.Thread(target=_start_loop, daemon=True)
    _loop_thread.start()
    time.sleep(0.2)

    private_key = os.environ["OG_PRIVATE_KEY"]
    llm_client = og.LLM(private_key=private_key)

    try:
        approval = llm_client.ensure_opg_approval(min_allowance=0.5)
        print(f"OPG approval: {approval}")
    except Exception as e:
        print(f"Approval warning: {e}")

    OG_OK = True
    print("OG connected")
except Exception as e:
    print(f"Demo mode: {e}")


def probe_models():
    global WORKING_MODEL
    if not OG_OK or llm_client is None:
        return
    print("Probing models...")
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
            ))
            raw = extract_raw(result)
            if raw and raw.strip():
                WORKING_MODEL = model
                print(f"Using model: {name}")
                return
        except Exception as e:
            print(f"  FAIL {name}: {e}")
    print("No working model found.")


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
    for attr in dir(result):
        if attr.startswith('_'):
            continue
        try:
            val = getattr(result, attr)
            if callable(val):
                continue
            if isinstance(val, str) and ('<JSON>' in val or '"risk_score"' in val):
                return val
        except:
            pass
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


def call_llm(messages, retries=3):
    global WORKING_MODEL
    if not OG_OK or llm_client is None:
        return {"error": "OpenGradient not available"}

    if WORKING_MODEL is None:
        probe_models()
    if WORKING_MODEL is None:
        return {"error": "No working model found"}

    last_error = ""
    for attempt in range(retries):
        try:
            print(f"LLM attempt {attempt+1} | model: {WORKING_MODEL}")
            result = _run(llm_client.chat(
                model=WORKING_MODEL,
                messages=messages,
                max_tokens=3000,
                temperature=0.3,
            ))
            raw = extract_raw(result)
            if not raw.strip():
                last_error = "Empty response"
                time.sleep(2)
                continue
            parsed = parse_json(raw)
            if "error" in parsed:
                last_error = parsed["error"]
                time.sleep(1)
                continue
            tx = getattr(result, "transaction_hash", None)
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
                time.sleep(2)

    return {"error": f"All attempts failed: {last_error}"}


@app.route("/")
def index():
    return jsonify({"status": "ok", "service": "LexGuard"})


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

    print(f"\nAnalyzing document | type: '{doc_type}'")

    user_content = []

    if pdf_base64:
        try:
            user_content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_base64
                }
            })
            print("PDF attached")
        except Exception as e:
            print(f"PDF error: {e}")
            return jsonify({"error": "Failed to process PDF"}), 400

    if doc_text:
        user_content.append({"type": "text", "text": f"LEGAL DOCUMENT:\n\n{doc_text}"})

    user_content.append({"type": "text", "text": f"Document type: {doc_type}\n\nAnalyze and return the JSON."})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content if len(user_content) > 1 else user_content[0]["text"]}
    ]

    result = call_llm(messages)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    print(f"LexGuard on :{port} | OG: {'live' if OG_OK else 'demo'}")
    app.run(host="0.0.0.0", port=port, debug=False)


# Запуск через gunicorn — пробинг моделей в фоне
def on_starting(server):
    if OG_OK:
        threading.Thread(target=probe_models, daemon=True).start()
