import os
import sys
from lmdeploy import (
    GenerationConfig,
    TurbomindEngineConfig,
    PytorchEngineConfig,
    ChatTemplateConfig,
)
from typing import Any, Dict, AsyncGenerator
from lmdeploy.archs import get_task
from loguru import logger
from gpt_server.model_backend.base import ModelBackend

if sys.platform == "linux":
    # 防止Python c库没有加载导致lmdeploy pytorch后端报错
    os.environ["C_INCLUDE_PATH"] = "/usr/include/python3.8:" + (
        os.environ.get("C_INCLUDE_PATH", "")
    )
    os.environ["LUS_INCLUDE_PATH"] = "/usr/include/python3.8:" + (
        os.environ.get("LUS_INCLUDE_PATH", "")
    )
backend_map = {
    "lmdeploy-pytorch": "pytorch",  # pytorch后端
    "lmdeploy-turbomind": "turbomind",  # turbomind后端
}


class LMDeployBackend(ModelBackend):
    def __init__(self, model_path) -> None:
        backend = backend_map[os.getenv("backend")]
        enable_prefix_caching = bool(os.getenv("enable_prefix_caching", False))
        max_model_len = os.getenv("max_model_len", None)
        gpu_memory_utilization = float(os.getenv("gpu_memory_utilization", 0.8))
        dtype = os.getenv("dtype", "auto")
        logger.info(f"后端 {backend}")
        if backend == "pytorch":
            backend_config = PytorchEngineConfig(
                tp=int(os.getenv("num_gpus", "1")),
                dtype=dtype,
                session_len=int(max_model_len) if max_model_len else None,
                enable_prefix_caching=enable_prefix_caching,
                cache_max_entry_count=gpu_memory_utilization,
            )
        if backend == "turbomind":
            backend_config = TurbomindEngineConfig(
                tp=int(os.getenv("num_gpus", "1")),
                enable_prefix_caching=enable_prefix_caching,
                session_len=int(max_model_len) if max_model_len else None,
                dtype=dtype,
                cache_max_entry_count=gpu_memory_utilization,
            )
        pipeline_type, pipeline_class = get_task(model_path)
        chat_template_config = ChatTemplateConfig(model_name=model_path)
        logger.info(f"模型架构：{pipeline_type}")
        self.async_engine = pipeline_class(
            model_path=model_path,
            backend=backend,
            backend_config=backend_config,
            chat_template_config=chat_template_config,
        )

    async def stream_chat(self, params: Dict[str, Any]) -> AsyncGenerator:
        prompt = params.get("prompt", "")
        logger.info(prompt)
        messages = params["messages"]
        request_id = params.get("request_id", "0")
        temperature = float(params.get("temperature", 0.8))
        top_p = float(params.get("top_p", 0.8))
        top_k = params.get("top_k", 50)

        max_new_tokens = min(int(params.get("max_new_tokens", 1024 * 8)), 1024 * 4)
        stop_str = params.get("stop", None)
        stop_token_ids = params.get("stop_words_ids", None) or []
        presence_penalty = float(params.get("presence_penalty", 0.0))
        frequency_penalty = float(params.get("frequency_penalty", 0.0))
        request = params.get("request", None)
        # Handle stop_str
        stop = set()
        if isinstance(stop_str, str) and stop_str != "":
            stop.add(stop_str)
        elif isinstance(stop_str, list) and stop_str != []:
            stop.update(stop_str)
        # input_ids = params.get("input_ids")
        # prompt_token_ids = input_ids.tolist()[0]
        # make sampling params in vllm
        top_p = max(top_p, 1e-5)
        gen_config = GenerationConfig(
            do_sample=True,
            top_p=top_p,
            temperature=temperature,
            max_new_tokens=max_new_tokens,  # 存在问题
            top_k=50 if top_k == -1 else top_k,
            stop_words=list(stop),
            skip_special_tokens=True,
            response_format=params["response_format"],
        )
        logger.info(f"request_id {int(request_id)}")
        results_generator = self.async_engine.generate(
            messages=messages, session_id=int(request_id), gen_config=gen_config
        )
        text_outputs = ""
        async for request_output in results_generator:
            if await request.is_disconnected():
                # Abort the request if the client disconnects.
                await self.async_engine.stop_session(session_id=request_id)
            text_outputs += request_output.response

            usage = {
                "prompt_tokens": request_output.input_token_len,
                "completion_tokens": request_output.generate_token_len,
                "total_tokens": request_output.input_token_len
                + request_output.generate_token_len,
            }
            ret = {
                "text": text_outputs,
                "error_code": 0,
                "usage": usage,
                "finish_reason": request_output.finish_reason,
            }
            yield ret
        logger.info(text_outputs)
        logger.info(usage)
