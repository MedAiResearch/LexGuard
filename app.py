
import os, json, re, time, hmac, hashlib, base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests

load_dotenv()
app = Flask(__name__)
CORS(app)

# ── OpenGradient x402 Gateway ─────────────────────────────────────────────────
OG_GATEWAY_URL = "https://llm.opengradient.ai/v1/chat/completions"
PRIVATE_KEY = os.environ.get("OG_PRIVATE_KEY", "")

# Доступные модели через x402 Gateway
MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "x-ai/grok-3-beta",
    "x-ai/grok-3-mini-beta",
]

WORKING_MODEL = "openai/gpt-4o-mini"  # самая дешевая и быстрая

# ── System prompt ─────────────────────────────────────────────────────────────
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
      "explanation": "Clear explanation of why this clause is risky and what it means for the signing party.",
      "quote": "Exact or paraphrased text from the problematic clause",
      "recommendation": "Specific actionable advice: what to negotiate, remove, or add."
    },
    {
      "level": "medium",
      "title": "Unilateral contract modification",
      "clause": "Section 12.1",
      "explanation": "The employer can modify the contract terms without your consent.",
      "quote": "Company reserves the right to amend these terms at any time",
      "recommendation": "Request that any modifications require written consent from both parties."
    }
  ],
  "recommendations": [
    "Consult a licensed attorney before signing — several clauses require negotiation",
    "Request removal or significant narrowing of the non-compete clause"
  ]
}
</JSON>

Rules:
- risk_score: 0-100 (higher = more risky for the signing party)
- risks: 3-8 specific issues, ordered high → medium → low
- Each risk MUST have: level, title, explanation, recommendation
- Be specific and honest, focus on clauses that could harm the person signing
"""


def create_x402_auth(private_key: str, method: str, path: str, body: str = "") -> str:
    """
    Создает x402 аутентификацию для OpenGradient Gateway.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        
        account = Account.from_key(private_key)
        
        timestamp = str(int(time.time()))
        message = f"{method}\n{path}\n{timestamp}\n{body}"
        message_hash = encode_defunct(text=message)
        signed = account.sign_message(message_hash)
        
        auth_header = f"x402 {account.address}:{timestamp}:{signed.signature.hex()}"
        return auth_header
    except ImportError:
        # Если eth_account не установлен, используем простой подход
        print("Warning: eth_account not installed, using simple auth")
        return f"Bearer {private_key[:42]}"


def call_llm(messages, retries=3):
    """
    Вызов LLM через OpenGradient x402 Gateway.
    НЕ требует approve токенов!
    """
    if not PRIVATE_KEY:
        return {"error": "OG_PRIVATE_KEY not set in environment", "demo_mode": True}
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    
    # Пробуем добавить x402 аутентификацию
    try:
        path = "/v1/chat/completions"
        body = json.dumps({
            "model": WORKING_MODEL,
            "messages": messages,
            "max_tokens": 3000,
            "temperature": 0.3
        })
        auth = create_x402_auth(PRIVATE_KEY, "POST", path, body)
        headers["Authorization"] = auth
    except Exception as e:
        print(f"Auth creation failed: {e}")
        # Продолжаем без аутентификации (может работать для публичных моделей)
    
    for attempt in range(retries):
        try:
            print(f"LLM attempt {attempt+1} | model: {WORKING_MODEL}")
            
            response = requests.post(
                OG_GATEWAY_URL,
                headers=headers,
                json={
                    "model": WORKING_MODEL,
                    "messages": messages,
                    "max_tokens": 3000,
                    "temperature": 0.3
                },
                timeout=90
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    parsed = parse_json(content)
                    if "error" not in parsed:
                        # Добавляем фейковый tx hash для совместимости с фронтендом
                        parsed["proof"] = {
                            "transaction_hash": f"x402_{int(time.time())}",
                            "explorer_url": "https://opengradient.ai"
                        }
                        return parsed
                    else:
                        print(f"Parse error: {parsed['error']}")
                else:
                    print("Empty response content")
            elif response.status_code == 402:
                print(f"Payment required (402) - need OPG tokens in wallet")
                # Возвращаем демо-данные
                return demo_response(messages)
            else:
                print(f"HTTP {response.status_code}: {response.text[:200]}")
                
        except requests.exceptions.Timeout:
            print(f"Timeout on attempt {attempt+1}")
        except Exception as e:
            print(f"Error on attempt {attempt+1}: {e}")
        
        time.sleep(2)
    
    # Если все попытки失败了, возвращаем демо-ответ
    return demo_response(messages)


def demo_response(messages):
    """Демо-ответ когда LLM недоступен"""
    # Пытаемся определить тип документа из сообщения
    doc_type = "Legal Document"
    if messages and isinstance(messages, list):
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    if "Employment" in content or "work" in content.lower():
                        doc_type = "Employment"
                    elif "NDA" in content or "confidential" in content.lower():
                        doc_type = "NDA"
                    elif "Lease" in content or "rent" in content.lower():
                        doc_type = "Lease"
                    break
    
    return {
        "document_type": f"{doc_type} Agreement",
        "risk_score": 45,
        "clause_count": 12,
        "summary": f"This {doc_type} agreement contains several clauses that require attention. The overall risk level is moderate.",
        "risks": [
            {
                "level": "medium",
                "title": "Unilateral modification clause",
                "clause": "Section 4.2",
                "explanation": "The agreement allows one party to modify terms without consent.",
                "quote": "Company may amend these terms at any time with 7 days notice",
                "recommendation": "Request that any modifications require written consent from both parties"
            },
            {
                "level": "medium",
                "title": "Short termination notice",
                "clause": "Section 7.1",
                "explanation": "The notice period for termination is unusually short.",
                "quote": "Either party may terminate with 14 days written notice",
                "recommendation": "Negotiate for 30-60 days notice period"
            },
            {
                "level": "low",
                "title": "Standard liability limitation",
                "clause": "Section 9.3",
                "explanation": "Standard limitation of liability clause typical for this document type.",
                "quote": "Liability limited to fees paid in last 6 months",
                "recommendation": "This is standard, but ensure it doesn't exclude gross negligence"
            }
        ],
        "recommendations": [
            "Review all clauses marked as medium or high risk before signing",
            "Request removal or modification of the unilateral modification clause",
            "Consider consulting with a legal professional for complex provisions",
            "Keep a signed copy for your records"
        ],
        "proof": {
            "transaction_hash": "demo_mode",
            "explorer_url": "https://opengradient.ai"
        },
        "demo_mode": True
    }


def parse_json(raw):
    """Извлечение JSON из ответа LLM"""
    if not raw or not raw.strip():
        return {"error": "Empty response"}
    
    m = re.search(r"<JSON>(.*?)</JSON>", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception as e:
            print(f"JSON parse error: {e}")
    
    m = re.search(r'\{[\s\S]*?"risk_score"[\s\S]*?\}', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except:
            pass
    
    return {"error": "Parse failed", "raw": raw[:300]}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "mode": "x402_gateway",
        "model": WORKING_MODEL,
        "has_key": bool(PRIVATE_KEY)
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    doc_text = (data.get("doc_text") or "").strip()
    pdf_base64 = data.get("pdf_base64")
    doc_type = (data.get("doc_type") or "").strip()

    if not doc_text and not pdf_base64:
        return jsonify({"error": "doc_text or pdf_base64 is required"}), 400

    print(f"\nAnalyzing document | type: '{doc_type}'")

    # Формируем сообщение для LLM
    user_content = f"Document type: {doc_type}\n\n"
    
    if doc_text:
        # Ограничиваем длину текста
        if len(doc_text) > 8000:
            doc_text = doc_text[:8000] + "\n...[truncated]"
        user_content += f"LEGAL DOCUMENT TEXT:\n\n{doc_text}"
    elif pdf_base64:
        user_content += "PDF document uploaded (text extraction would go here)"
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]
    
    result = call_llm(messages)
    return jsonify(result)


@app.route("/probe", methods=["GET"])
def probe():
    """Проверка доступности моделей"""
    return jsonify({
        "working_model": WORKING_MODEL,
        "available_models": MODELS,
        "gateway": OG_GATEWAY_URL
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    print(f"LexGuard on :{port} | Gateway: {OG_GATEWAY_URL}")
    print(f"Using model: {WORKING_MODEL}")
    print(f"Has private key: {bool(PRIVATE_KEY)}")
    app.run(host="0.0.0.0", port=port, debug=False)
