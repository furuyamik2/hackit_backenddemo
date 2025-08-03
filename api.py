# backend/api.py

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import random
import firebase_admin
from firebase_admin import credentials, firestore
import asyncio
import os
from dotenv import load_dotenv
import httpx 
import json
import textwrap

# .envファイルの読み込み
load_dotenv()


# Firebase Admin SDKの初期化
firebase_key_path = os.getenv("FIREBASE_KEY_PATH")
cred = credentials.Certificate(firebase_key_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

# FastAPIアプリケーションの初期化
app = FastAPI()

# CORS設定をFastAPIのappインスタンスに適用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# リクエストボディのデータモデルを定義
class CreateRoomRequest(BaseModel):
    username: str
    uid:str

class JoinRoomRequest(BaseModel):
    roomId: str
    username: str
    uid: str

class UpdateSettingsRequest(BaseModel):
    roomId: str
    topic: str
    duration: int # 時間は数値として受け取る

class GenerateAgendaRequest(BaseModel):
    topic: str
    total_duration: int

@app.post("/create_room")
async def create_room(request: CreateRoomRequest):
    try:
        username = request.username
        uid = request.uid
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")

        #重複しないルームIDの生成
        while True:
            room_id = str(random.randint(10000, 99999))
            room_ref = db.collection('rooms').document(room_id)
            doc = await asyncio.get_event_loop().run_in_executor(None, room_ref.get)
            if not doc.exists:
                break
        
        # ルームを作成し、参加者をマップ形式で保存
        room_ref.set({
            'creator_uid': request.uid,
            'participants': {
                request.uid: request.username
            },
            'createdAt': firestore.SERVER_TIMESTAMP
        })
        
        print(f"Room {room_id} created by {username}")
        return {"roomId": room_id}

    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error creating room: {e}")
        raise HTTPException(status_code=500, detail="Failed to create room")


@app.post("/join_room")
def join_room(request: JoinRoomRequest):
    room_ref = db.collection('rooms').document(request.roomId)

    try:
        # トランザクションを使って、データの読み込みと更新を安全に実行
        @firestore.transactional
        def update_in_transaction(transaction, room_ref, uid, username):
            snapshot = room_ref.get(transaction=transaction)
            
            if not snapshot.exists:
                return {"error": "not_found"}

            room_data = snapshot.to_dict()
            participants = room_data.get("participants", {})

            # 参加者が自分以外で、すでに5人以上いる場合は満員
            if uid not in participants and len(participants) >= 5:
                return {"error": "full"}
            
            # 参加者情報を更新 (同じuidなら上書き、新規なら追加される)
            transaction.update(room_ref, {
                f'participants.{uid}': username
            })
            return {} # 成功

        # トランザクションを実行
        result = update_in_transaction(db.transaction(), room_ref, request.uid, request.username)

        # エラーハンドリング
        if result.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="指定されたルームが見つかりません。")
        if result.get("error") == "full":
            raise HTTPException(status_code=403, detail="このルームは満員です。")
            
        print(f"User {request.username} ({request.uid}) joined room {request.roomId}")
        return {"roomId": request.roomId, "message": "Joined successfully"}

    except HTTPException as e:
        # 自分で投げたHTTPExceptionはそのまま送出
        raise e
    except Exception as e:
        print(f"Error joining room: {e}")
        raise HTTPException(status_code=500, detail="ルームへの参加に失敗しました。")

  
@app.post("/update_room_settings")
def update_room_settings(request: UpdateSettingsRequest):
    try:
        room_ref = db.collection('rooms').document(request.roomId)
        
        # データベースに議題、制限時間、そして「議論中」というステータスを保存
        room_ref.update({
            'topic': request.topic,
            'duration': request.duration,
            'status': 'discussing'  # この行が重要！
        })
        print(f"Room {request.roomId} status changed to 'discussing'")
        return {"message": "Settings updated and discussion started"}
    except Exception as e:
        print(f"Error updating room settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to update settings")
    
@app.post("/generate_agenda")
async def generate_agenda(request: GenerateAgendaRequest):
    """Gemini AIを使って、議題に基づいた議論の段取りを生成する"""
    if not db:
        raise HTTPException(status_code=500, detail="Database connection is not available.")
        
    API_KEY = os.getenv("GEMINI_API_KEY", "")
    if not API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEYが設定されていません。")
        
    GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={API_KEY}"

    # ▼▼▼【変更点】AIへの役割と指示を、より具体的で高度なものに修正 ▼▼▼
    prompt = textwrap.dedent(f"""
        あなたは、創造的なアイデアを引き出し、議論を生産的な結論に導くことを得意とする、世界クラスのファシリテーターです。
        与えられた議題と時間の中で、最高の成果を出せるような議論の段取りを設計してください。

        # 議題
        {request.topic}

        # 全体の時間
        {request.total_duration}分

        # 指示
        - 議論の基本的な流れである「発散（アイデアを広げる）」→「収束（アイデアをまとめる・決める）」を意識したステップを構成してください。
        - 各ステップには、ステップ名、参加者への問いかけ、分単位の時間配分を含めてください。
        - 時間配分の合計が、全体の時間 ({request.total_duration}分) と厳密に一致するようにしてください。
        - 「参加者への問いかけ」は、具体的で、参加者が何をすべきか明確にわかるオープンクエスチョン（はい/いいえで終わらない質問）にしてください。
        - 最初のステップは、参加者の緊張をほぐし、自由に意見を言えるような雰囲気を作る問いかけから始めてください。
    """)
    
    # ▼▼▼【変更点】信頼性を高めるため、スキーマ（応答形式の設計図）を導入 ▼▼▼
    agenda_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "step_name": {"type": "STRING"},
                "prompt_question": {"type": "STRING"},
                "allocated_time": {"type": "INTEGER"}
            },
            "required": ["step_name", "prompt_question", "allocated_time"]
        }
    }

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": agenda_schema # スキーマを指定
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(GEMINI_URL, json=payload)
            response.raise_for_status()
            
            result = response.json()
            
            # (安全なチェック処理はそのまま)
            if "candidates" not in result or not result["candidates"]:
                raise HTTPException(status_code=500, detail="AIからの応答がありませんでした。")

            candidate = result["candidates"][0]
            
            if candidate.get("finishReason") == "SAFETY":
                raise HTTPException(status_code=400, detail="議題が不適切と判断されたため、アジェンダを生成できませんでした。")
            
            if "content" not in candidate or "parts" not in candidate["content"]:
                raise HTTPException(status_code=500, detail="AIからの応答形式が正しくありません。")

            agenda_json_text = candidate["content"]["parts"][0]["text"]
            agenda = json.loads(agenda_json_text)
            
            print("✅ AIによるアジェンダ生成成功:", agenda)
            return agenda

    except httpx.HTTPStatusError as e:
        print(f"❌ Gemini API Error: {e.response.text}")
        raise HTTPException(status_code=500, detail="AIによるアジェンダ生成に失敗しました。")
    except json.JSONDecodeError:
        print(f"❌ JSON Decode Error: Failed to parse AI response: {agenda_json_text}")
        raise HTTPException(status_code=500, detail="AIの応答を解析できませんでした。")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail="予期せぬエラーが発生しました。")