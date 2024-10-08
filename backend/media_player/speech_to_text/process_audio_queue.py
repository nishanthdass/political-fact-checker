import os
import glob
from collections import deque
import time
import whisperx
import whisper
from pyannote.audio import Pipeline, Model, Inference
from dotenv import load_dotenv
from pyannote.core import Segment
import numpy as np
from scipy.spatial.distance import cosine

load_dotenv()

speaker_diarization = os.getenv("speaker_diarization")
pipeline = os.getenv("pipeline")
inference_model = os.getenv("inference_model")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
EMBEDDING_DIR = os.path.join(BASE_DIR, 'speech_to_text', 'embedding_data')
TEMP_DIR = os.path.join(BASE_DIR, 'speech_to_text', 'temp_audio_files') 

class ProcessAudioQueue:
    def __init__(self, temp_dir='temp_audio_files', session_id=None, device = None, model=None):
        self.session_id = session_id
        self.queue = deque()
        self.device = device
        self.model = model
        self.whisper_model = whisper.load_model("base.en")
        self.pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                        use_auth_token=pipeline)
        self.inference_model = Model.from_pretrained("pyannote/embedding", 
                        use_auth_token=inference_model)
        self.diarize_bank = {}

        self._load_files()

    def _load_files(self):
        """
        Load all files from the temporary directory that match the session ID
        and add their names to the queue.
        """
        session_prefix = f"{self.session_id}_"
        # Get full paths of matching files
        files = sorted(glob.glob(os.path.join(TEMP_DIR, f"{session_prefix}*.wav")))
        # Extract file names from paths
        file_names = [os.path.basename(f) for f in files]
        self.queue.extend(file_names)
        print(f"Loaded files into queue: {self.queue}")

    def enqueue(self, file_name):
        """
        Add a new file name to the queue.
        """
        self.queue.append(file_name)

    def dequeue(self):
        """
        Process the first file in the queue, then remove it.
        """
        if self.queue:
            file_name = self.queue.popleft()
            if file_name:
                try:
                    self.process_file(file_name)
                except Exception as e:
                    print(f"Error processing file {file_name}: {e}")
                    self._delete_file(file_name)
                self._delete_file(file_name)
        else:
            print("Queue is empty.")

    def process_file(self, file_name):
        """
        Process the file before deleting it.
        Custom processing logic can be implemented here.
        """
        try:
            full_path = os.path.join(TEMP_DIR, file_name)
            self.embed_transcribe_speakers(full_path)
        except Exception as e:
            print(f"Exception occurred while processing {file_name}: {e}")
            raise 

    def embed_transcribe_speakers(self, full_path):

        inference = Inference(self.inference_model, window="whole")
        audio = whisperx.load_audio(full_path)
        result = self.model.transcribe(audio, batch_size=16)
        align_model, metadata = whisperx.load_align_model(language_code="en", device=self.device)

        # # Align the transcription for word-level timing
        aligned_result = whisperx.align(result["segments"], align_model, metadata, full_path, self.device)
        diarize_model = whisperx.DiarizationPipeline(use_auth_token="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", device=self.device)
        diarize_segments = diarize_model(audio)
        aligned_result = whisperx.assign_word_speakers(diarize_segments, aligned_result)

        for segments in aligned_result["segments"]:
            phrases = {}
            for word in segments["words"]:
                if word["speaker"] not in phrases:
                    phrases[word["speaker"]] = {"text" : "", "start" : 0, "end" : 0}
                    phrases[word["speaker"]]["start"] = word["start"]
                phrases[word["speaker"]]["text"] += word["word"] + " "
                phrases[word["speaker"]]["end"] = word["end"]
            for phrase in phrases:
                segment = Segment(phrases[phrase]["start"], phrases[phrase]["end"])
                speaker_embedding = inference.crop(full_path, segment)
                similarities = self.recognize_speaker(speaker_embedding, phrases, phrase)
                speaker_similarity = max(similarities.values())
                speaker_name = max(similarities, key=similarities.get)

                if speaker_similarity < 0.1:
                    speaker_name = "Unknown"
                print(speaker_name, "(", speaker_similarity, ")",": ", phrases[phrase]["text"])
                

    def recognize_speaker(self, speaker_embedding, phrases, phrase):
        trump_embedding_path = os.path.join(EMBEDDING_DIR, 'trump_embedding.npy')
        trump_embedding = np.load(trump_embedding_path)
        kamala_embedding_path = os.path.join(EMBEDDING_DIR, 'kamala_embedding.npy')
        kamala_embedding = np.load(kamala_embedding_path)
        speaker = {"Trump" : 0, "Kamala" : 0}
        
        if speaker_embedding.ndim == 1:
            similarities = []
            for idx, trump_frame in enumerate(trump_embedding):
                similarity = 1 - cosine(speaker_embedding, trump_frame)
                similarities.append(similarity)
            mean_similarity = np.mean(similarities)
            speaker["Trump"] = mean_similarity

            similarities = []
            for idx, kamala_frame in enumerate(kamala_embedding):
                similarity = 1 - cosine(speaker_embedding, kamala_frame)
                similarities.append(similarity)
            mean_similarity = np.mean(similarities)
            speaker["Kamala"] = mean_similarity
            
            
            return speaker
        else:
            print(f"Speaker embedding is not 1D: {speaker_embedding.ndim}D")
            return None



    def _delete_file(self, file_name, max_retries=5, wait_time=0.5):
        """
        Delete the specified file from the file system, waiting until it is accessible.
        """
        full_path = os.path.join(TEMP_DIR, file_name)

        retries = 0
        while retries < max_retries:
            try:
                if os.path.exists(full_path):
                    with open(full_path, 'r+'):
                        pass

                    os.remove(full_path)
                    return
                else:
                    print(f"File does not exist: {full_path}")
                    return
            except PermissionError:
                retries += 1
                print(f"PermissionError: File is in use. Retrying {retries}/{max_retries}...")
                time.sleep(wait_time)
            except FileNotFoundError:
                print(f"File not found during deletion: {full_path}")
                return
        print(f"Failed to delete file after {max_retries} attempts: {full_path}")

    def clear_queue(self):
        """
        Clear the queue and delete all files associated with the session ID.
        """
        while self.queue:
            file_name = self.queue.popleft()
            self._delete_file(file_name)
        print("Queue cleared and all session files deleted.")

    def list_files(self):
        """
        List all files currently in the queue.
        """
        return list(self.queue)
