# backend/ws_server.py

from fastapi import FastAPI
import firebase_admin
from firebase_admin import credentials, firestore
import socketio
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

# Socket.IOサーバーの初期化
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins=["http://localhost:5173","https://hackit-black.vercel.app/"]
)

# FastAPIのインスタンス
app = FastAPI()

# Socket.IOのASGIアプリをFastAPIアプリケーションにマウント
app.mount("/socket.io", socketio.ASGIApp(sio)) # ルートパスにマウントすることで、'/'でSocket.IOが動作する

# Socket.IOのASGIアプリを定義
sio_app = socketio.ASGIApp(socketio_server=sio, other_asgi_app=app)


# Socket.IOのイベントハンドラ
@sio.on('connect')
async def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.on('disconnect')
async def handle_disconnect(sid):
    # sidからルームとユーザーを特定し、データベースから削除するロジック
    # (ハッカソンMVPでは実装を省略してもよい)
    pass

@sio.on('join_room')
async def handle_join_room(sid, data):
    print("Received data from client:", data)
    room_id = data.get('roomId')
    # username = data.get('username')
    uid = data.get('uid')

    if not all([room_id, uid]):
        print("Error: Invalid data received for join_room.")
        return

    await sio.enter_room(sid, room_id)
    print(f"User {sid} ({uid}) joined room {room_id}")

    # Firebaseから最新のルーム情報を取得
    room_ref = db.collection('rooms').document(room_id)
    doc = await asyncio.to_thread(room_ref.get) # 非同期でブロッキング処理を実行

    if doc.exists:
        room_data = doc.to_dict()
        # participants = [p['username'] for p in room_data.get('participants', [])]
        participants_map = room_data.get('participants', {})
        participants_list = list(participants_map.values())
        creator_uid = room_data.get('creator_uid') # 作成者のUIDも取得
        
        # 参加者リストと作成者のUIDを全員に送信
        await sio.emit('participants_update', {'participants': participants_list, 'creator_uid': creator_uid}, room=room_id)
    
@sio.on('leave_room')
async def handle_leave_room(sid, data):
    room_id = data.get('roomId')
    uid = data.get('uid')

    if not all([room_id, uid]):
        return

    room_ref = db.collection('rooms').document(room_id)

    try:
        # --- ▼▼▼【ここからが大きな変更点】▼▼▼ ---
        @firestore.transactional
        def leave_in_transaction(transaction, room_ref, uid_to_remove):
            snapshot = room_ref.get(transaction=transaction)
            if not snapshot.exists:
                return {'status': 'not_found'}

            room_data = snapshot.to_dict()
            creator_uid = room_data.get('creator_uid')

            # ホストが退出した場合、ルームを削除
            if uid_to_remove == creator_uid:
                print(f"👑 ホスト({uid_to_remove})が退出したため、ルーム'{room_id}'を削除します。")
                transaction.delete(room_ref)
                return {'status': 'host_left'}

            # 一般参加者が退出した場合
            participants = room_data.get('participants', {})
            if uid_to_remove in participants:
                del participants[uid_to_remove]

            if not participants:
                print(f"🔥 参加者が0人になったため、ルーム'{room_id}'を削除します。")
                transaction.delete(room_ref)
                return {'status': 'deleted_empty'}
            else:
                transaction.update(room_ref, {'participants': participants})
                return {'status': 'updated'}
        
        result = await asyncio.to_thread(
            leave_in_transaction, db.transaction(), room_ref, uid
        )
        status = result.get('status')

        # Socket.IOのルームからクライアントを退出させる
        await sio.leave_room(sid, room_id)

        if status == 'host_left':
            # ホストが退出したことを残りの全員に通知
            print(f"📢 ルーム'{room_id}'の参加者に解散を通知します。")
            await sio.emit('room_closed', {'message': 'ホストが退出したため、ルームは解散しました。'}, room=room_id)
            # ルームにいる全員の接続をサーバー側から切断
            await sio.close_room(room_id)
        elif status == 'updated':
            # 参加者リストの更新を通知
            await handle_join_room(sid, {'roomId': room_id, 'uid': 'system_update'})

    except Exception as e:
        print(f"❌ ルーム退出処理中にエラーが発生しました: {e}")

sio.on('start_discussion')
async def handle_start_discussion(sid, data):
    room_id = data.get('roomId')
    if not room_id:
        return
    
    print(f"📢 議論開始の合図を受信。ルーム'{room_id}'の全員に通知します。")
    
    # ルームにいる全員に'discussion_started'イベントを送信
    await sio.emit('discussion_started', {'roomId': room_id}, room=room_id)

@sio.on('join_discussion_room')
async def handle_join_discussion_room(sid, data):
    """議論ページのユーザーを、チャット用の部屋に参加させる"""
    room_id = data.get('roomId')
    if not room_id:
        return
    
    discussion_room_name = f"discussion_{room_id}"
    await sio.enter_room(sid, discussion_room_name)
    print(f"✅ Client {sid} joined discussion room '{discussion_room_name}'")


@sio.on('send_message')
async def handle_send_message(sid, data):
    """ユーザーからメッセージを受け取り、同じ部屋の全員に送信する"""
    room_id = data.get('roomId')
    message = data.get('message')
    if not all([room_id, message]):
        return
    
    discussion_room_name = f"discussion_{room_id}"
    
    # メッセージを、送信者(sid)以外の全員に送信する
    # skip_sid=sid が、この機能の核心です
    await sio.emit('new_message', message, room=discussion_room_name, skip_sid=sid)
    print(f"💬 Sent message to room '{discussion_room_name}' (excluding sender)")

@sio.on('finish_step')
async def handle_finish_step(sid, data):
    """ユーザーがステップを完了したことを記録し、現在の進捗を全員に通知する"""
    room_id = data.get('roomId')
    uid = data.get('uid')
    if not all([room_id, uid]):
        return

    room_ref = db.collection('rooms').document(room_id)
    discussion_room_name = f"discussion_{room_id}"

    try:
        # ▼▼▼ トランザクションのロジックを全面的に書き換えます ▼▼▼
        @firestore.transactional
        def update_in_transaction(transaction, room_ref, user_uid):
            snapshot = room_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None

            room_data = snapshot.to_dict()

            # 1. まず現在のデータをPythonの変数にコピーする
            participants_map = room_data.get('participants', {})
            finished_users_map = room_data.get('finished_users', {})

            # 2. Pythonの変数上で、完了者に自分を追加する
            finished_users_map[user_uid] = True

            # 3. 更新されたPython変数を使って、最新の人数を計算する
            participants_count = len(participants_map)
            finished_count = len(finished_users_map)
            
            # 4. 最後に、更新済みの完了者リストをデータベースに書き込む
            transaction.update(room_ref, {'finished_users': finished_users_map})
            
            # 5. 計算済みの人数を返す
            return {'finished_count': finished_count, 'total_participants': participants_count}

        result = await asyncio.to_thread(
            update_in_transaction, db.transaction(), room_ref, uid
        )
        # ▲▲▲ ここまでが変更箇所 ▲▲▲

        if result:
            print(f"👍 Progress update for room '{room_id}': {result['finished_count']} / {result['total_participants']}")
            # 全員に進捗状況をブロードキャスト
            await sio.emit('progress_update', result, room=discussion_room_name)

    except Exception as e:
        print(f"❌ Error in handle_finish_step: {e}")

@sio.on('reset_progress_for_next_step')
async def handle_reset_progress(sid, data):
    """次のステップに進むために、完了者リストをリセットする"""
    room_id = data.get('roomId')
    if not room_id:
        return
    
    try:
        room_ref = db.collection('rooms').document(room_id)
        # finished_users フィールドを空のマップで上書きしてリセット
        await asyncio.to_thread(room_ref.update, {'finished_users': {}})
        print(f"⏩ Progress reset for next step in room '{room_id}'")
    except Exception as e:
        print(f"❌ Error in handle_reset_progress: {e}")