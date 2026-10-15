# Copyright(c) 2025 NVIDIA Corporation. All rights reserved.

# NVIDIA Corporation and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA Corporation is strictly prohibited.

import os
import json
import uvicorn
import yaml
import redis
from pathlib import Path
from dotenv import load_dotenv, find_dotenv, dotenv_values
from fastapi import FastAPI
from loguru import logger
from .config import Config
from .serializer import TokkioUIWebSocketSerializer
from .rag_multimodal_response_processor import RAGMultimodalResponseProcessor
from .otel import tracer_provider, is_otel_collector_up
from pydantic import BaseModel  # KYORA: config section model, see customization notebook 11
from .kyora_processor import KyoraProcessor  # KYORA: overlay processor (tokkio-kyora/src/kyora_processor.py)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# from pipecat.audio.vad.vad_analyzer import VADParams
# from pipecat.audio.vad.silero import SileroVADAnalyzer
# from pipecat.frames.frames import Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from nvidia_pipecat.services.nvidia_llm import NvidiaLLMService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from .tokkio_llm import TokkioNvidiaLLMService, TokkioOpenAILLMService
from .tokkio_rag import TokkioNvidiaRAGService
from pipecat.services.openai.llm import OpenAILLMService

from nvidia_pipecat.utils.message_broker import MessageBrokerConfig
from nvidia_pipecat.transports.network.ace_fastapi_websocket import (
    ACETransport,
    ACETransportParams,
)
from nvidia_pipecat.transports.services.ace_controller.routers.websocket_router import (
    router as websocket_router,
)
from nvidia_pipecat.pipeline.ace_pipeline_runner import (
    PipelineMetadata,
    ACEPipelineRunner,
)
from nvidia_pipecat.services.riva_speech import RivaASRService, RivaTTSService
from nvidia_pipecat.services.nvidia_rag import NvidiaRAGService
from nvidia_pipecat.services.elevenlabs import ElevenLabsTTSServiceWithEndOfSpeech
# from nvidia_pipecat.processors.audio_util import AudioRecorder
from nvidia_pipecat.services.animation_graph_service import AnimationGraphService
from nvidia_pipecat.services.audio2face_3d_service import Audio2Face3DService
from nvidia_pipecat.processors.user_presence import UserPresenceProcesssor
from nvidia_pipecat.processors.proactivity import ProactivityProcessor
from nvidia_pipecat.processors.posture_provider import PostureProviderProcessor
from nvidia_pipecat.processors.transcript_synchronization import (
    UserTranscriptSynchronization,
    BotTranscriptSynchronization,
)
from nvidia_pipecat.utils.logging import setup_default_ace_logging
from nvidia_pipecat.transports.services.ace_controller.routers.register_apis_router import (
    router as register_apis_router,
)
from nvidia_pipecat.processors.nvidia_context_aggregator import (
    create_nvidia_context_aggregator,
    # NvidiaTTSResponseCacher, # Uncomment to enable speculative speech processing
)

load_dotenv(override=True)

dotenv_path = find_dotenv()
if dotenv_path:
    logger.info(f"Found .env file at: {dotenv_path}")
    for env_name, env_value in dotenv_values(dotenv_path).items():
        logger.info(f"{env_name}={env_value}")

setup_default_ace_logging(level="DEBUG")

config = Config(
    **yaml.safe_load(
        Path(os.getenv("CONFIG_PATH")).read_text())
)

AnimationGraphService.pregenerate_animation_databases(config.AnimationGraphService)
# KYORA ---------------------------------------------------------------------------
class KyoraProcessorConfig(BaseModel):  # KYORA: mirrors the KyoraProcessor section of configs/config.yaml
    events_url: str = ""  # KYORA: Kyora base URL reachable from the cluster
    token: str = ""  # KYORA: Kyora hub token
    narrate: bool = True  # KYORA
    show_views: bool = True  # KYORA
    reconnect_s: float = 3.0  # KYORA
class KyoraConfig(Config):  # KYORA: extends the vendored Config without editing config.py
    KyoraProcessor: KyoraProcessorConfig = KyoraProcessorConfig()  # KYORA: section is optional
kyora_cfg = KyoraConfig(**yaml.safe_load(Path(os.getenv("CONFIG_PATH")).read_text())).KyoraProcessor  # KYORA
kyora_events_url = kyora_cfg.events_url or os.getenv("KYORA_EVENTS_URL", "")  # KYORA: config first, env fallback
kyora_token = kyora_cfg.token or os.getenv("KYORA_HUB_TOKEN", "")  # KYORA
logger.info(f"Kyora overlay: events_url={kyora_events_url or '(not configured)'} narrate={kyora_cfg.narrate} show_views={kyora_cfg.show_views}")  # KYORA
# KYORA ---------------------------------------------------------------------------

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = redis.Redis(
    host=redis_url.split("//")[1].split(':')[0],
    port=redis_url.split("//")[1].split(':')[1]
)

if os.getenv("OTEL_SDK_DISABLED", "false") == "false" and not is_otel_collector_up():
    logger.error("Please make sure the OTEL collector endpoint is healthy or set env OTEL_SDK_DISABLED=true")
    raise Exception("OTEL collector not healthy")

async def create_pipeline_task(pipeline_metadata: PipelineMetadata):
    tracer = tracer_provider.get_tracer(__name__)
    try:
        trace_carrier = json.loads(json.loads(redis_client.hget("trace_contexts", pipeline_metadata.stream_id)))
        trace_context = TraceContextTextMapPropagator().extract(trace_carrier)
    except Exception:
        logger.error("Unable to find or invalid trace context for stream {}".format(pipeline_metadata.stream_id))
        trace_context = None

    with tracer.start_as_current_span("ace-pipeline", context=trace_context) as span:
        transport = ACETransport(
            websocket=pipeline_metadata.websocket,
            params=ACETransportParams(
                serializer=TokkioUIWebSocketSerializer(),
                rtsp_url=pipeline_metadata.rtsp_url,
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )

        a2f = Audio2Face3DService(
            target=os.getenv("A2F_GRPC_URL", "0.0.0.0:50010"),
            sample_rate=16000,
            bit_per_sample=16,
            send_silence_on_start=True,
        )


        animgraph = AnimationGraphService(
            animation_graph_rest_url=os.getenv("ANIMGRAPH_URL", "http://localhost:8020"),
            animation_graph_grpc_target=os.getenv("ANIMGRAPH_GRPC_URL", "ia-animation-graph-microservice:51000"),
            message_broker_config=MessageBrokerConfig(
                "redis", os.getenv("REDIS_URL", "redis://localhost:6379")
            ),
            config=config.AnimationGraphService,
        )

        posture_provider_processor = PostureProviderProcessor()

        user_presence_processor = UserPresenceProcesssor(
            welcome_msg=config.UserPresenceProcesssor.welcome_message, farewell_msg=config.UserPresenceProcesssor.farewell_message
        )
        proactivity_processor = ProactivityProcessor(
            timer_duration=config.ProactivityProcessor.timer_duration,
            default_message=config.ProactivityProcessor.default_message
        )

        if config.Pipeline.llm_processor == 'NvidiaLLMService':
            llm = TokkioNvidiaLLMService(
                api_key=os.getenv("NVIDIA_API_KEY"),
                base_url=config.NvidiaLLMService.base_url,
                model=config.NvidiaLLMService.model,
                filler=config.Pipeline.filler,
                time_delay=config.Pipeline.time_delay,
            )

        if config.Pipeline.llm_processor == 'OpenAILLMService':
            llm = TokkioOpenAILLMService(
                api_key=os.getenv("OPENAI_API_KEY"),
                model=config.OpenAILLMService.model,
                filler=config.Pipeline.filler,
                time_delay=config.Pipeline.time_delay,
            )

        if config.Pipeline.llm_processor == 'NvidiaRAGService':
            llm = TokkioNvidiaRAGService(
                collection_name=config.NvidiaRAGService.collection_name,
                rag_server_url=config.NvidiaRAGService.rag_server_url,
                use_knowledge_base=config.NvidiaRAGService.use_knowledge_base,
                max_tokens=config.NvidiaRAGService.max_tokens,
                suffix_prompt=config.NvidiaRAGService.suffix_prompt,
                filler=config.Pipeline.filler,
                time_delay=config.Pipeline.time_delay,
            )

        # For Nim use:
        # stt = RivaASRService(
        #     api_key=os.getenv("NVIDIA_API_KEY"),
        #     language="en-US",
        #     sample_rate=16000,
        #     model="parakeet-1.1b-en-US-asr-streaming-asr-bls-ensemble",
        # )

        riva_server_ip = os.getenv("RIVA_SERVER_URL", "localhost:50052")
        if riva_server_ip != "localhost:50052":
            riva_server_ip.replace("http://", "")
        stt = RivaASRService(
            server=riva_server_ip,
            language="en-US",
            sample_rate=16000,
            model="parakeet-1.1b-en-US-asr-streaming-silero-vad-asr-bls-ensemble",
        )
        tts = ElevenLabsTTSServiceWithEndOfSpeech(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID", "cgSgspJ2msm6clMCkdW9"),
            sample_rate=16000,
            model = "eleven_flash_v2_5",
            stability = 0.3,
            speed = 0.97,
            similarity_boost = 0.85
        )

        # Used to synchronize the user and bot transcripts in the UI
        stt_transcript_synchronization = UserTranscriptSynchronization()
        tts_transcript_synchronization = BotTranscriptSynchronization()

        avatar_name = config.OpenAILLMContext.name
        avatar_prompt_template = config.OpenAILLMContext.prompt

        # Insert the name into the dynamic prompt
        avatar_prompt = avatar_prompt_template.replace("{name}", avatar_name)

        messages = [
            {
                "role": "system",
                "content": avatar_prompt,
            },
        ]

        context = OpenAILLMContext(messages)
    
        # Comment out the below line when enabling Speculative Speech Processing
        context_aggregator = llm.create_context_aggregator(context)

        # Uncomment the below line to enable speculative speech processing
        # nvidia_context_aggregator = create_nvidia_context_aggregator(context, send_interims=True)
        # Uncomment the below line to enable speculative speech processing
        # nvidia_tts_response_cacher = NvidiaTTSResponseCacher()

        rag_multimodal_response_processor = RAGMultimodalResponseProcessor()
        kyora_processor = KyoraProcessor(  # KYORA
            kyora_events_url, kyora_token,  # KYORA
            narrate=kyora_cfg.narrate, show_views=kyora_cfg.show_views, reconnect_s=kyora_cfg.reconnect_s,  # KYORA
        )  # KYORA

        pipeline = Pipeline(
            [
                transport.input(),
                # audio_recorder,
                user_presence_processor,
                stt, # Speech-To-Text
                stt_transcript_synchronization,
                # Comment out the below line when enabling Speculative Speech Processing
                context_aggregator.user(),
                # Uncomment the below line to enable speculative speech processing
                # nvidia_context_aggregator.user(),
                llm,
                proactivity_processor,
                tts,
                # Uncomment the below line to enable speculative speech processing
                # nvidia_tts_response_cacher,  # For caching TTS response when Speculative Speech Processing is enabled
                tts_transcript_synchronization,
                rag_multimodal_response_processor,
                kyora_processor,  # KYORA: after RAG custom views, before A2F; narration is queued at the pipeline head
                a2f,
                # audio_recorder,
                posture_provider_processor,
                animgraph,
                # Comment out the below line when enabling Speculative Speech Processing
                context_aggregator.assistant(),
                # Uncomment the below line to enable speculative speech processing
                # nvidia_context_aggregator.assistant(),
                transport.output(),  # Websocket output to client
            ]
        )

        task = PipelineTask(
            pipeline,
            cancel_on_idle_timeout=False,
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
                start_metadata={"stream_id" : pipeline_metadata.stream_id},
            ),
        )

        kyora_processor.attach_task(task)  # KYORA: task.queue_frame(TTSSpeakFrame) reaches the TTS upstream of us
        return task

app = FastAPI()
app.include_router(websocket_router)
app.include_router(register_apis_router)
runner = ACEPipelineRunner(pipeline_callback=create_pipeline_task, enable_rtsp=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8100)
