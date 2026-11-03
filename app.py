import json
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, Response, jsonify, render_template, request
from mlx_lm import stream_generate
from mlx_lm.sample_utils import make_sampler

from config import (
    DEFAULT_CWD,
    DEFAULT_MODEL_PATH,
    DEFAULT_PERSONA,
    HOST,
    PORT,
    build_system_prompt,
    is_conversational_or_greeting,
    select_relevant_tools,
)
from database import (
    db_create_session,
    db_delete_session,
    db_get_messages,
    db_get_sessions,
    db_save_message,
    init_db,
)
from engine import engine
from tools import execute_tool, format_tools_for_prompt, get_registered_tools

app = Flask(__name__)

ENGINE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def parse_lenient_action_json(raw_str: str) -> dict:
    cleaned = raw_str.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    start_idx = cleaned.find("{")
    if start_idx == -1:
        raise json.JSONDecodeError("No JSON object '{' found", cleaned, 0)
    decoder = json.JSONDecoder(strict=False)
    obj, _ = decoder.raw_decode(cleaned[start_idx:])
    return obj


def extract_think_and_reply(
    raw_text: str, prompt_ended_with_think: bool, is_final_pass: bool = False
) -> tuple[str, str, bool]:
    """
    Separates <think>...</think> from the visible reply.
    Returns: (think_text, answer_text, is_currently_thinking)
    """
    text = raw_text or ""

    # Case 1: Both <think> and </think> exist in generated text
    if "<think>" in text and "</think>" in text:
        m = re.search(r"<think>([\s\S]*?)</think>", text)
        think_part = m.group(1).strip() if m else ""
        ans_part = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
        return think_part, ans_part, False

    # Case 2: Prompt ended with <think>, so generated text has `...thought...</think>answer`
    if "</think>" in text:
        think_part, ans_part = text.split("</think>", 1)
        return think_part.replace("<think>", "").strip(), ans_part.strip(), False

    # Case 3: Prompt ended with <think>, and model generated <action> directly without </think>
    if prompt_ended_with_think and re.search(r"<action>\s*\{", text):
        idx = text.find("<action>")
        think_part = text[:idx].replace("<think>", "").strip()
        ans_part = text[idx:].strip()
        return think_part, ans_part, False

    # Case 4: Tokenizer appended <think> to the end of the prompt, model still inside <think>
    if prompt_ended_with_think:
        think_part = text.replace("<think>", "").strip()
        # In both streaming and final pass, text generated within <think> is thought, NEVER visible answer!
        return think_part, "", not is_final_pass

    # Case 5: Model explicitly emitted <think>, and hasn't emitted </think> yet
    if "<think>" in text:
        idx = text.find("<think>")
        before = text[:idx].strip()
        after = text[idx + 7 :].strip()
        return after, before, not is_final_pass

    return "", text.strip(), False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/init", methods=["GET"])
def api_init():
    sessions = db_get_sessions()
    if not sessions:
        sessions = [db_create_session("New Chat")]
    return jsonify(
        {
            "default_model": engine.model_path or DEFAULT_MODEL_PATH,
            "cwd": DEFAULT_CWD,
            "persona": DEFAULT_PERSONA,
            "tools": get_registered_tools(),
            "sessions": sessions,
            "active_mem_mb": engine.get_active_ram_mb(),
        }
    )


@app.route("/api/session/new", methods=["POST"])
def api_new_session():
    s = db_create_session("New Chat")
    engine.clear_cache()
    engine.reset_kv_cache()
    return jsonify(s)


@app.route("/api/session/<sid>", methods=["GET"])
def api_get_session(sid):
    return jsonify({"id": sid, "messages": db_get_messages(sid)})


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_del_session(sid):
    db_delete_session(sid)
    sessions = db_get_sessions()
    if not sessions:
        sessions = [db_create_session("New Chat")]
    return jsonify({"sessions": sessions})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    STOP_EVENT.set()
    return jsonify({"status": "stopped"})


@app.route("/api/stream", methods=["POST"])
def api_stream():
    data = request.json or {}
    session_id = data.get("session_id")
    user_prompt = data.get("prompt", "").strip()
    model_path = data.get("model_path", DEFAULT_MODEL_PATH)
    cwd = data.get("cwd", DEFAULT_CWD)
    persona = data.get("persona", DEFAULT_PERSONA)
    temp = float(data.get("temperature", 0.65))
    top_p = float(data.get("top_p", 0.9))
    enable_thinking = bool(data.get("enable_thinking", True))
    action_budget = int(data.get("action_token_limit", 1536))
    final_budget = int(data.get("final_token_limit", 4096))
    max_steps = int(data.get("max_steps", 8))
    obs_limit = int(data.get("obs_limit", 4000))
    enabled_tools = data.get("tools", [])

    db_save_message(session_id, "user", user_prompt)

    def event_stream():
        STOP_EVENT.clear()
        logs = []
        steps = []
        turn_images = []
        accumulated_thoughts = []
        executed_signatures = set()
        collected_observations = []

        def emit(event_type: str, payload: dict):
            return f"data: {json.dumps({'type': event_type, **payload})}\n\n"

        def emit_log(tag: str, msg: str):
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            entry = {"ts": ts, "tag": tag, "msg": msg}
            logs.append(entry)
            return emit("log", entry)

        with ENGINE_LOCK:
            try:
                yield emit_log("ENGINE", "Checking local MLX weights...")
                engine.ensure_loaded(model_path)
            except Exception as e:
                yield emit("error", {"message": str(e)})
                return

            db_msgs = db_get_messages(session_id)

            # Discover previous tools if user asks to "try again" or confirm/proceed
            previous_tools = []
            for m in reversed(db_msgs):
                if m["role"] == "assistant":
                    if m.get("steps"):
                        for s in m["steps"]:
                            parts = s.get("label", "").split("|")
                            if len(parts) > 1:
                                t_name = parts[1].split(":")[0].strip()
                                if t_name and t_name not in previous_tools:
                                    previous_tools.append(t_name)
                    content = m.get("content", "")
                    if re.search(r"\b(rm |ls |mkdir |touch |find |mv |cp |git |terminal|```(?:bash|sh|zsh)?)\b", content):
                        if "run_terminal" not in previous_tools:
                            previous_tools.append("run_terminal")
                    if previous_tools:
                        break
                elif m["role"] == "user":
                    prev_user_tools = select_relevant_tools(m.get("content", ""), enabled_tools)
                    for pt in prev_user_tools:
                        if pt not in previous_tools:
                            previous_tools.append(pt)
                    if previous_tools:
                        break

            is_conv = is_conversational_or_greeting(user_prompt)
            scoped_tools = [] if is_conv else select_relevant_tools(user_prompt, enabled_tools, previous_tools=previous_tools)
            allow_tools = len(scoped_tools) > 0
            effective_max_steps = max_steps if allow_tools else 1

            if is_conv:
                yield emit_log("ROUTER", "Conversational / Greeting intent: instant reply without tools.")
            elif allow_tools:
                yield emit_log("ROUTER", f"Tools enabled: {', '.join(scoped_tools)}")
            else:
                yield emit_log("ROUTER", "Direct chat (no external tools required).")

            # Check if user asks to download more from previous site without explicitly writing the URL
            site_context_note = ""
            if allow_tools and "download_media" in scoped_tools and "http" not in user_prompt:
                found_url = None
                for m in reversed(db_msgs):
                    urls = re.findall(r"https?://[^\s,;\"'<>]+", m.get("content", ""))
                    if not urls and m.get("steps"):
                        for s in m["steps"]:
                            urls.extend(re.findall(r"https?://[^\s,;\"'<>]+", s.get("output", "")))
                    if urls:
                        found_url = urls[0]
                        break
                if found_url:
                    parsed_f = urlparse(found_url)
                    site_root = f"{parsed_f.scheme}://{parsed_f.netloc}/"
                    site_context_note = (
                        f"\n\n[CONTEXT NOTE FOR ASSISTANT]: The user is asking to download from or explore "
                        f"the site used previously: {site_root} (previous post was {found_url}). "
                        f"In your download_media call, pass '{site_root}' as the url; "
                        f"the tool will automatically crawl and download the video posts from that site!"
                    )

            raw_history = []
            for m in db_msgs:
                # Re-inject saved tool observations only when follow-up tool queries might need them.
                # Never bloat casual conversation/greetings with old tool observations!
                if m["role"] == "assistant" and m.get("steps") and not is_conv:
                    obs_summary = "\n".join(
                        f"[Previous Tool Result ({s['label']})]:\n{s['output'][:1200]}"
                        for s in m["steps"]
                    )
                    raw_history.append(
                        {"role": "user", "content": f"<observation>\n{obs_summary}\n</observation>"}
                    )
                raw_history.append({"role": m["role"], "content": m["content"]})

            sampler = make_sampler(temp=temp, top_p=top_p)
            final_answer = ""
            tool_context = {"cwd": cwd, "obs_limit": obs_limit, "timeout": 45}

            for step_num in range(1, effective_max_steps + 1):
                if STOP_EVENT.is_set():
                    yield emit_log("STOP", "Interrupted by user.")
                    break

                # NEVER waste thinking tokens on casual chat or greetings!
                # Thinking is reserved strictly for planning tool steps when tools are enabled.
                step_thinking = (not is_conv) and enable_thinking and allow_tools

                tool_schemas_str = format_tools_for_prompt(scoped_tools) if allow_tools else ""
                sys_prompt = build_system_prompt(
                    cwd, tool_schemas_str, persona, allow_tools=allow_tools, is_conversational=is_conv
                )
                if site_context_note and allow_tools:
                    sys_prompt += site_context_note

                pruned_history = []
                total_len = len(raw_history)
                for idx, m in enumerate(raw_history):
                    c = m["content"]
                    if m["role"] == "user" and c.startswith("<observation>") and (total_len - idx) > 4:
                        c = c[:1000] + "\n...[older observation compressed]...\n</observation>"
                    pruned_history.append({"role": m["role"], "content": c})

                chat_messages = [{"role": "system", "content": sys_prompt}] + pruned_history

                try:
                    prompt_str = engine.tokenizer.apply_chat_template(
                        chat_messages,
                        tokenize=False,
                        add_generation_prompt=True,
                        enable_thinking=step_thinking,
                    )
                except TypeError:
                    prompt_str = engine.tokenizer.apply_chat_template(
                        chat_messages,
                        tokenize=False,
                        add_generation_prompt=True,
                    )

                prompt_ended_with_think = step_thinking and ("<think>" in prompt_str[-25:])

                full_tokens = engine.tokenizer.encode(prompt_str)
                delta_tokens, reused_count = engine.get_delta_tokens(full_tokens, session_id)

                base_step_budget = action_budget if (allow_tools and step_num < effective_max_steps) else final_budget
                step_budget = base_step_budget + (768 if step_thinking else 0)

                if reused_count > 0:
                    yield emit_log(
                        f"STEP {step_num}",
                        f"KV Cache Hit: Reused {reused_count} tokens, prefilling {len(delta_tokens)} delta tokens (Thinking={step_thinking})...",
                    )
                else:
                    yield emit_log(
                        f"STEP {step_num}",
                        f"Prefilling {len(delta_tokens)} tokens (Thinking={step_thinking}, Budget={step_budget})...",
                    )

                generated_text = ""
                gen_token_ids = []
                token_count = 0
                t0 = time.perf_counter()
                ttft_logged = False
                gen_tps = 0.0

                try:
                    for resp in stream_generate(
                        engine.model,
                        engine.tokenizer,
                        prompt=delta_tokens,
                        max_tokens=step_budget,
                        sampler=sampler,
                        prompt_cache=engine.kv_cache,
                    ):
                        if STOP_EVENT.is_set():
                            break

                        if not ttft_logged:
                            ttft_logged = True
                            ttft_ms = (time.perf_counter() - t0) * 1000
                            yield emit_log(
                                "PREFILL",
                                f"TTFT: {ttft_ms:.0f} ms ({resp.prompt_tps:.1f} prompt tok/s)",
                            )

                        generated_text += resp.text
                        gen_token_ids.append(resp.token)
                        token_count += 1
                        gen_tps = resp.generation_tps

                        think_part, ans_part, is_thinking = extract_think_and_reply(
                            generated_text, prompt_ended_with_think, is_final_pass=False
                        )

                        # Only check for <action> / </action> inside ans_part (OUTSIDE <think>)!
                        if not is_thinking:
                            # If tools are NOT allowed and the model tries to output <action>, immediately cut it off
                            if not allow_tools and ("<action>" in ans_part or re.search(r"<action>\s*\{?", ans_part)):
                                act_idx = generated_text.find("<action>")
                                if act_idx != -1:
                                    generated_text = generated_text[:act_idx].strip()
                                ans_part = re.split(r"<action>", ans_part)[0].strip()
                                yield emit(
                                    "token",
                                    {
                                        "think": combined_think,
                                        "text": ans_part,
                                        "is_thinking": False,
                                        "tps": round(gen_tps, 1),
                                    },
                                )
                                break

                            if "</action>" in ans_part:
                                cutoff = generated_text.rfind("</action>") + len("</action>")
                                generated_text = generated_text[:cutoff]
                                think_part, ans_part, is_thinking = extract_think_and_reply(
                                    generated_text, prompt_ended_with_think, is_final_pass=True
                                )
                                combined_think = "\n\n".join(
                                    filter(
                                        None,
                                        accumulated_thoughts
                                        + ([f"[Step {step_num}] {think_part}"] if think_part else []),
                                    )
                                )
                                yield emit(
                                    "token",
                                    {
                                        "think": combined_think,
                                        "text": ans_part,
                                        "is_thinking": False,
                                        "tps": round(gen_tps, 1),
                                    },
                                )
                                break

                        combined_think = "\n\n".join(
                            filter(
                                None,
                                accumulated_thoughts
                                + ([f"[Step {step_num}] {think_part}"] if think_part else []),
                            )
                        )

                        yield emit(
                            "token",
                            {
                                "think": combined_think,
                                "text": ans_part,
                                "is_thinking": is_thinking,
                                "tps": round(gen_tps, 1),
                            },
                        )
                except GeneratorExit:
                    STOP_EVENT.set()
                    break

                engine.cached_tokens = full_tokens + gen_token_ids

                elapsed = time.perf_counter() - t0
                yield emit_log(
                    "DECODE",
                    f"Decoded {token_count}/{step_budget} tokens in {elapsed:.2f}s ({gen_tps:.1f} tok/s)",
                )

                step_think, text_without_think, _ = extract_think_and_reply(
                    generated_text, prompt_ended_with_think, is_final_pass=True
                )
                if step_think:
                    accumulated_thoughts.append(f"[Step {step_num}] {step_think}")

                if STOP_EVENT.is_set():
                    final_answer = (text_without_think or generated_text).strip() + "\n\n*[Stopped by user]*"
                    break

                if not allow_tools:
                    # Strip any action tag remnants completely
                    clean_ans = re.sub(
                        r"<action\b[^>]*>[\s\S]*?(?:</action>|$)", "", text_without_think, flags=re.DOTALL
                    ).strip()
                    clean_ans = re.sub(r"<action\b[^>]*$", "", clean_ans).strip()
                    clean_ans = re.sub(r'\{"\s*$', "", clean_ans).strip()
                    clean_ans = re.sub(r'\{[^{}]*$', "", clean_ans).strip()
                    final_answer = clean_ans
                    break

                # If the model exhausted budget during thought without generating any action or answer
                if not text_without_think.strip():
                    if step_num < effective_max_steps:
                        yield emit_log("SYNTHESIZE", "Thinking completed. Generating direct response...")
                        allow_tools = False
                        yield emit("clear_stream", {})
                        continue
                    else:
                        break

                if re.search(r"<action>\s*\{", text_without_think) and "</action>" not in text_without_think:
                    yield emit_log("WARN", "Hit token limit mid-action. Requesting concise JSON...")
                    raw_history.append({"role": "assistant", "content": text_without_think})
                    raw_history.append(
                        {
                            "role": "user",
                            "content": "<observation>Error: Action cut off due to length. Emit a concise <action>{...}</action> block.</observation>",
                        }
                    )
                    yield emit("clear_stream", {})
                    continue

                action_match = re.search(r"<action>\s*(\{.*?\})\s*</action>", text_without_think, re.DOTALL)

                if action_match:
                    raw_history.append({"role": "assistant", "content": text_without_think})
                    raw_json = action_match.group(1).strip()

                    try:
                        payload = parse_lenient_action_json(raw_json)
                        tool_name = payload.get("tool", "")

                        sig = json.dumps(payload, sort_keys=True)
                        if sig in executed_signatures or tool_name not in scoped_tools:
                            yield emit_log(
                                "LOCK",
                                f"Blocked duplicate or out-of-scope call to '{tool_name}'. Moving to synthesis...",
                            )
                            scoped_tools = [t for t in scoped_tools if t != tool_name]
                            if not scoped_tools:
                                allow_tools = False
                            raw_history.append(
                                {
                                    "role": "user",
                                    "content": f"<observation>[SYSTEM_NOTICE] '{tool_name}' was already run. Answer the user directly now using the results above.</observation>",
                                }
                            )
                            yield emit("clear_stream", {})
                            continue

                        executed_signatures.add(sig)
                        yield emit_log("ACTION", f"Calling '{tool_name}' -> {raw_json[:110]}")

                        t_tool = time.perf_counter()
                        obs = execute_tool(tool_name, payload, tool_context)
                        label = f"Step {step_num} | {tool_name}: {raw_json[:80]}"

                        img_match = re.search(
                            r"\[IMAGE_GALLERY_JSON\](.*?)\[/IMAGE_GALLERY_JSON\]", obs, re.DOTALL
                        )
                        if img_match:
                            try:
                                parsed_imgs = json.loads(img_match.group(1))
                                turn_images.extend(parsed_imgs)
                                yield emit("images", {"images": parsed_imgs})
                            except Exception:
                                pass
                            obs = re.sub(
                                r"\[IMAGE_GALLERY_JSON\].*?\[/IMAGE_GALLERY_JSON\]\n?",
                                "",
                                obs,
                                flags=re.DOTALL,
                            )

                        ms = (time.perf_counter() - t_tool) * 1000
                        yield emit_log(
                            "OBSERVE", f"'{tool_name}' returned {len(obs)} chars in {ms:.0f} ms"
                        )

                        step_obj = {"label": label, "output": obs}
                        steps.append(step_obj)
                        collected_observations.append(obs)
                        yield emit("step", step_obj)

                        # Single-use tools (like search) are evicted once executed;
                        # multi-step tools (run_terminal, python_exec, write_file, read_file) remain available
                        repeatable_tools = {"run_terminal", "python_exec", "write_file", "read_file"}
                        if tool_name not in repeatable_tools:
                            scoped_tools = [t for t in scoped_tools if t != tool_name]

                        # If web_search completed and no file/terminal operations were requested, lock tools immediately
                        if tool_name == "web_search":
                            has_file_ops = any(
                                t in scoped_tools
                                for t in ("write_file", "run_terminal", "python_exec", "fetch_webpage", "read_file")
                            )
                            if not has_file_ops:
                                allow_tools = False
                                yield emit_log("SYNTHESIZE", "Search complete. Locking tools & writing direct response...")

                        if not scoped_tools:
                            allow_tools = False

                        raw_history.append(
                            {"role": "user", "content": f"<observation>\n{obs}\n</observation>"}
                        )
                        yield emit("clear_stream", {})
                        continue

                    except Exception as err:
                        yield emit_log("ERROR", f"JSON Parse Error ({err}). Retrying...")
                        raw_history.append(
                            {
                                "role": "user",
                                "content": f"<observation>Error: Invalid JSON inside <action> ({err}). Output valid JSON.</observation>",
                            }
                        )
                        yield emit("clear_stream", {})
                        continue
                else:
                    final_answer = text_without_think.strip()
                    break
            else:
                if not final_answer:
                    final_answer = "Reached maximum autonomous steps limit."

            # Ensure final_answer is thoroughly cleaned of any leaked thinking tags or action residue
            clean_ans = re.sub(r"<think>[\s\S]*?</think>", "", final_answer).strip()
            clean_ans = re.sub(r"^</think>\s*", "", clean_ans).strip()
            clean_ans = re.sub(r"<action\b[^>]*>[\s\S]*?(?:</action>|$)", "", clean_ans, flags=re.DOTALL).strip()
            clean_ans = re.sub(r"<action\b[^>]*$", "", clean_ans).strip()
            clean_ans = re.sub(r'\{"\s*$', "", clean_ans).strip()
            clean_ans = re.sub(r'\{[^{}]*$', "", clean_ans).strip()
            if clean_ans.lower().startswith("thinking process:"):
                parts = re.split(r"\n\s*\n", clean_ans, maxsplit=1)
                if len(parts) > 1:
                    clean_ans = parts[1].strip()
            if clean_ans:
                final_answer = clean_ans

            engine.clear_cache()
            active_mb = engine.get_active_ram_mb()
            yield emit_log("COMPLETE", f"Finished. Unified RAM: {active_mb} MB")

            full_think_str = "\n\n".join(accumulated_thoughts)
            db_save_message(
                session_id,
                "assistant",
                final_answer,
                think=full_think_str,
                logs=logs,
                steps=steps,
                images=turn_images,
            )

            yield emit(
                "done",
                {
                    "think": full_think_str,
                    "content": final_answer,
                    "active_mem_mb": active_mb,
                    "sessions": db_get_sessions(),
                },
            )

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    init_db()
    print(f"Aria Venice-UI Agent running on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
