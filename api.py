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

# .envファイルの読み込み
load_dotenv()

# Firebase Admin SDKの初期化
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
    
    # Gemini APIのエンドポイントとキー（Renderの環境変数に設定することを推奨）
    # キーが空文字列の場合、Canvas環境では自動的にキーが提供されます
    API_KEY = os.getenv("GEMINI_API_KEY", "") 
    GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={API_KEY}"

    # AIへの指示（プロンプト）
    prompt = f"""
あなたは議論を円滑に進める優秀なファシリテーターです。
以下の議題と全体の時間に基づいて、議論の段取りをステップ・バイ・ステップで提案してください。

# 議題
{request.topic}

# 全体の時間
{request.total_duration}分

# 出力形式のルール
- 必ずJSON形式で、ステップの配列として出力してください。
- 各ステップには `step_name` (ステップ名), `prompt_question` (参加者への問いかけ), `allocated_time` (分単位の時間配分) の3つのキーを含めてください。
- `allocated_time` の合計が、全体の時間 ({request.total_duration}分) と一致するように調整してください。
- `prompt_question` は、参加者が具体的なアクションを取りやすい、明確で分かりやすい問いかけにしてください。

JSON出力例:
[
  {{
    "step_name": "アイデアの発散",
    "prompt_question": "この議題について、まずは思いつくアイデアを自由に5つずつ挙げてください。",
    "allocated_time": 10
  }},
  {{
    "step_name": "アイデアの整理とグループ化",
    "prompt_question": "出てきたアイデアを似ているもの同士でグループ分けし、それぞれに名前を付けてみましょう。",
    "allocated_time": 15
  }}
]
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(GEMINI_URL, json=payload)
            response.raise_for_status() # エラーがあれば例外を発生させる
            
            result = response.json()
            agenda_json_text = result["candidates"][0]["content"]["parts"][0]["text"]
            agenda = json.loads(agenda_json_text)
            
            print("✅ AIによるアジェンダ生成成功:", agenda)
            return agenda

    except httpx.HTTPStatusError as e:
        print(f"❌ Gemini API Error: {e.response.text}")
        raise HTTPException(status_code=500, detail="AIによるアジェンダ生成に失敗しました。")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail="予期せぬエラーが発生しました。")