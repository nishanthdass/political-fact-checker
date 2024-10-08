from fastapi import APIRouter, HTTPException, Request, Depends, BackgroundTasks
from app.utilities.stream_response import handle_full_request, handle_range_request
from urllib.parse import quote
import os
from media_player.audio_player import AudioPlayer
from app.session_middleware import get_session_id
from media_player.speech_to_text.process_audio_queue import ProcessAudioQueue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from typing import Dict
from threading import Lock
import whisperx

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
VIDEO_DIR = os.path.join(BASE_DIR, 'media_player', 'video_clips')
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, 'media_player', 'speech_to_text', 'temp_audio_files')

audio_player = AudioPlayer(temp_dir=TEMP_AUDIO_DIR)

active_threads: Dict[str, Observer] = {}
active_threads_lock = Lock()

device = "cpu"
model = whisperx.load_model("base", device, compute_type="float32")


@router.get("/videos")
async def get_videos():
    try:
        files = os.listdir(VIDEO_DIR)
        video_files = [
            {"name": file, "url": f"http://localhost:8000/videos/{quote(file)}"}
            for file in files
            if file.endswith((".mp4", ".webm"))  # Filter by video file types
        ]
        return video_files
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/videos/{video_name}")
async def get_video(video_name: str, request: Request):
    video_path = os.path.join(VIDEO_DIR, video_name)
    
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video not found")

    file_size = os.path.getsize(video_path)
    range_header = request.headers.get('range')

    if range_header:
        return handle_range_request(video_path, file_size, range_header)
    else:
        return handle_full_request(video_path, file_size)
    
class FileCreationHandler(FileSystemEventHandler):
    def __init__(self, session_id, device=None, model=None):
        super().__init__()
        self.device = device
        self.model = model
        self.audio_queue = ProcessAudioQueue(session_id=session_id, device=self.device, model=self.model)

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('.wav'):
            file_name = os.path.basename(event.src_path)
            self.audio_queue.enqueue(file_name)
            self.audio_queue.dequeue()
            
# Dictionary to keep track of active event handlers for each session
active_event_handlers: Dict[str, FileCreationHandler] = {}

@router.post("/audio-control")
async def control_audio(request: Request, background_tasks: BackgroundTasks, session_id: str = Depends(get_session_id)):
    data = await request.json()
    action = data.get('action')
    time = data.get('time')
    video_name = data.get('videoName')

    audio_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Audio not found")

    try:
        audio_player.set_session(session_id)
        event_handler = None

        if action == 'play':
            audio_player.play(audio_path, time)
            with active_threads_lock:
                if session_id in active_threads:
                    active_threads[session_id].stop()
                    active_threads[session_id].join()
                event_handler = FileCreationHandler(session_id, device, model)
                observer = Observer()
                observer.schedule(event_handler, path=TEMP_AUDIO_DIR, recursive=False)
                observer.start()
                active_threads[session_id] = observer
                active_event_handlers[session_id] = event_handler
        elif action == 'pause':
            audio_player.pause()

    except Exception as e:
        print(f"Error processing audio control command: {e}")

    return {"status": "ok"}
