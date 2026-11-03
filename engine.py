from pathlib import Path
import mlx.core as mx
from mlx_lm.models.cache import make_prompt_cache
from mlx_lm.utils import load_model, load_tokenizer


class MLXEngine:
    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.model_path = None
        self.kv_cache = None
        self.cached_tokens = []
        self.active_session_id = None

    def ensure_loaded(self, raw_path: str) -> str:
        clean = raw_path.strip()
        candidate = Path(clean).expanduser()

        if not candidate.is_absolute():
            stripped = clean.lstrip("./").lstrip("/")
            for sc in [
                (Path.cwd() / clean).resolve(),
                (Path.cwd().parent / stripped).resolve(),
                (Path(__file__).resolve().parent / clean).resolve(),
                (Path.home() / stripped).resolve(),
                (Path.home() / "Desktop" / stripped).resolve(),
            ]:
                if sc.is_dir() and (sc / "config.json").is_file():
                    candidate = sc
                    break
            else:
                candidate = (Path.cwd() / clean).resolve()
        else:
            candidate = candidate.resolve()

        if not candidate.is_dir() or not (candidate / "config.json").is_file():
            # If not a valid local model directory, check if it's a Hugging Face Hub model ID
            try:
                from mlx_lm.utils import _download
                candidate = _download(clean)
            except Exception as hf_err:
                raise FileNotFoundError(
                    f"Valid MLX model directory not found locally at '{candidate}' "
                    f"and failed to fetch Hugging Face repository '{clean}': {hf_err}\n"
                    f"Tip: Specify a valid local folder with config.json and .safetensors weights, "
                    f"or a valid Hugging Face repo ID (e.g., 'mlx-community/Qwen2.5-7B-Instruct-4bit')."
                )

        resolved_str = str(candidate)
        if self.model is not None and self.model_path == resolved_str:
            return resolved_str

        self.clear_cache()
        model_res = load_model(candidate, lazy=False)
        self.model = model_res[0] if isinstance(model_res, tuple) else model_res
        self.tokenizer = load_tokenizer(
            candidate, tokenizer_config_extra={"trust_remote_code": True}
        )
        self.model_path = resolved_str
        self.reset_kv_cache()
        return resolved_str

    def reset_kv_cache(self):
        if self.model is not None:
            self.kv_cache = make_prompt_cache(self.model)
        self.cached_tokens = []

    def get_delta_tokens(self, full_tokens: list[int], session_id: str) -> tuple[list[int], int]:
        if self.active_session_id != session_id or self.kv_cache is None:
            self.reset_kv_cache()
            self.active_session_id = session_id
            return full_tokens, 0

        n_cached = len(self.cached_tokens)
        if n_cached > 0 and len(full_tokens) > n_cached and full_tokens[:n_cached] == self.cached_tokens:
            return full_tokens[n_cached:], n_cached

        self.reset_kv_cache()
        self.active_session_id = session_id
        return full_tokens, 0

    @staticmethod
    def clear_cache():
        try:
            mx.clear_cache()
        except AttributeError:
            mx.metal.clear_cache()

    @staticmethod
    def get_active_ram_mb() -> float:
        return round(mx.get_active_memory() / (1024 * 1024), 1)


engine = MLXEngine()