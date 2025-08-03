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