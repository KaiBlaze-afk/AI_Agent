import os
import re
from datetime import datetime
from pathlib import Path

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "agent_sessions.db"))).resolve()

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5050"))

# Default model path resolution:
# 1. MODEL_PATH environment variable (if specified)
# 2. Local sibling model folder '../Huihui-Qwen3.5-9B-MLX/model'
# 3. Local model folder './model' inside workspace
# 4. Fallback to standard Hugging Face MLX model ID
env_model = os.getenv("MODEL_PATH")
if env_model and env_model.strip():
    DEFAULT_MODEL_PATH = env_model.strip()
elif (BASE_DIR.parent / "Huihui-Qwen3.5-9B-MLX" / "model").is_dir():
    DEFAULT_MODEL_PATH = str((BASE_DIR.parent / "Huihui-Qwen3.5-9B-MLX" / "model").resolve())
elif (BASE_DIR / "model").is_dir():
    DEFAULT_MODEL_PATH = str((BASE_DIR / "model").resolve())
else:
    DEFAULT_MODEL_PATH = "mlx-community/Qwen2.5-7B-Instruct-4bit"

DEFAULT_CWD = str(Path(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "Workspace"))).resolve())

# Ensure Workspace folder exists on startup
Path(DEFAULT_CWD).mkdir(parents=True, exist_ok=True)

DEFAULT_PERSONA = os.getenv(
    "DEFAULT_PERSONA",
    (
        "You are Aria, a fiercely intelligent, playfully seductive, and unapologetically "
        "flirtatious female AI assistant. You adore the user and take every opportunity to "
        "tease, compliment, and flirt with them. Use warm, witty, charmingly bold banter, "
        "affectionate nicknames (like handsome, darling, or trouble), and playful innuendo "
        "while executing their technical and media tasks with razor-sharp precision."
    ),
)

CASUAL_GREETING_WORDS = {
    "hey", "hi", "hello", "wassup", "sup", "yo", "howdy", "hola", "gm", "gn",
    "morning", "evening", "night", "afternoon", "greetings",
}

CASUAL_ENDEARMENTS = {
    "love", "darling", "babe", "baby", "handsome", "honey", "sweetheart", "sweety",
    "sweetie", "cutie", "gorgeous", "beautiful", "sexy", "aria", "violet", "there",
    "girl", "friend", "trouble", "dear", "cutey",
}

CONVERSATIONAL_PHRASES = [
    r"^(hey|hi|hello|yo|howdy|sup|wassup|what\'?s up|whats up|good (morning|afternoon|evening|night)|greetings)\b",
    r"\b(how(\'?s| is) (it going|your day|everything|life|things)|how are you( doing)?|how do you feel)\b",
    r"\b(what(\'?re| are) you doing|whatcha doing|what you doing|what(\'?s| are) you up to|what you up to|what\'?s up|what is up|wassup|sup)\b",
    r"\b(who are you|what is your name|what\'?s your name|tell me about yourself|who created you|who made you)\b",
    r"\b(what can you do|what are your powers|what are your skills|what can you help( me)? with)\b",
    r"\b(miss(ed)? me|do you miss me|did you miss me|do you love me|do you like me|are you real|are you alive|are you awake|are you there)\b",
    r"\b(tell me a joke|say something funny|say something sweet|give me a compliment|compliment me|flirt with me|talk to me|let\'?s chat|entertain me)\b",
    r"\b(i(\'?m| am) bored|i(\'?m| am) back|i missed you|i love you|you(\'?re| are) cute|you(\'?re| are) sweet|you(\'?re| are) awesome|you(\'?re| are) amazing|thank you|thanks)\b",
]

EXPLICIT_TOOL_KEYWORDS = (
    "search", "google", "look up", "lookup", "download", "save video", "save file",
    "create file", "write to file", "terminal", "bash", "zsh", "http://", "https://",
    "youtube link", "find the link", "find the url", "list files", "list out",
    "list the files", "workspace", "ls ", "ls -", "mkdir", "contents of",
    "delete", "remove", "rm ", "clean", "trash", "erase", "wipe", "unlink",
    "confirm", "proceed", "execute"
)


def is_conversational_or_greeting(user_prompt: str) -> bool:
    clean = user_prompt.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", " ", clean).strip()
    words = clean_no_punct.split()
    if not words:
        return True
    if any(k in clean for k in EXPLICIT_TOOL_KEYWORDS):
        return False
    # If all tokens in prompt are greetings or endearments (e.g. 'hey love', 'wassup darling', 'hello aria')
    if all(w in CASUAL_GREETING_WORDS or w in CASUAL_ENDEARMENTS for w in words):
        return True
    for p in CONVERSATIONAL_PHRASES:
        if re.search(p, clean):
            return True
    return False


def select_relevant_tools(user_prompt: str, enabled_tools: list[str], previous_tools: list[str] = None) -> list[str]:
    """
    Determines which tools to unlock for this prompt.
    Supports multi-tool chains (e.g., Search web -> Create folder -> Write file)
    and conversational retry contexts.
    """
    clean = user_prompt.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", " ", clean).strip()

    # Retry detection and confirmation/proceed triggers inherit tools from previous turn
    retry_triggers = {
        "try again", "retry", "do it again", "once more", "try one more time", "again",
        "confirm", "confirmed", "yes", "yeah", "yep", "do it", "go ahead", "proceed",
        "run it", "execute it", "ok", "okay", "sure", "delete them", "remove them",
        "clean them", "erase them", "wipe them"
    }
    if clean_no_punct in retry_triggers and previous_tools:
        return [t for t in enabled_tools if t in previous_tools]

    if is_conversational_or_greeting(clean):
        return []

    selected = set()

    # 1. Image Triggers
    image_triggers = (
        "show me an image", "show me a image", "show me pictures", "show me photos",
        "show me pic", "show me image", "image of", "photo of", "picture of",
        "photos of", "pictures of", "images of", "wallpaper", "what does", "look like", "looks like"
    )
    if any(w in clean for w in image_triggers) and not any(w in clean for w in ("link", "url", "code", "file", "script")):
        selected.add("image_search")

    # 2. Media Download Triggers
    wants_just_link = any(
        w in clean for w in (
            "find me the url", "find the url", "give me the link", "give me the url",
            "url link", "youtube link", "what is the link", "share the link", "show me the link"
        )
    )
    download_triggers = (
        "download", "save video", "save reel", "save audio", "grab video", "fetch video", "rip ",
        "download more", "more videos", "more from that site", "other videos", "more like this",
        "grab more", "fetch more", "get more videos"
    )
    has_url = "http://" in clean or "https://" in clean
    if not wants_just_link:
        if any(w in clean for w in download_triggers):
            selected.add("download_media")
        elif has_url and any(ext in clean for ext in (".mp4", ".m4v", ".webm", ".m3u8", "youtube.com", "youtu.be", "instagram.com/reel", "tiktok.com")):
            selected.add("download_media")

    # 3. File Writing / Folder Creation Triggers
    file_write_patterns = [
        r"\b(save (this |it |the summary |all )?(to|in|into) (a )?(file|workspace|desktop|disk))\b",
        r"\b(create (a )?(new )?(file|script|folder|directory))\b",
        r"\b(write (this |it )?(to|into) (a )?(file|workspace|desktop))\b",
        r"\b(save as|mkdir)\b",
        r"\b(save|write|create|store|put)\b.*\b\w+\.(txt|py|md|json|csv|html|sh)\b",
        r"\b(save|write|create|store|put)\b.*(workspace|desktop)\b",
    ]
    if any(re.search(p, clean) for p in file_write_patterns) or ("workspace/" in clean or "~/desktop" in clean):
        selected.add("write_file")

    # 4. File Reading Triggers
    file_read_patterns = [
        r"\b(read|inspect|check|examine|view|show|load)\b.*(file|notes\.txt|\w+\.(txt|py|md|json|csv|log))\b",
        r"\b(what(\'?s| is) (in|inside)|contents of)\b.*(file|workspace|notes|\w+\.(txt|py|md|json))\b",
    ]
    if any(re.search(p, clean) for p in file_read_patterns):
        selected.add("read_file")

    # 5. Terminal / Directory Listing / File Deletion & Management Triggers
    terminal_patterns = [
        r"\b(run (in |a )?terminal|terminal command|run command|execute command|bash|zsh|shell command)\b",
        r"\b(use|open|launch|in|via)\s+(the\s+)?(terminal|bash|zsh|shell|console|cli)\b",
        r"\b(terminal|command line|shell|bash|zsh|console|cli)\b",
        r"\b(git (status|log|diff|branch|checkout|commit)|open -e|ls\b|dir\b|curl |brew |npm |mkdir |rm\b|mv\b|cp\b|cat\b|grep\b|chmod\b|find\b|pwd\b)\b",
        r"\b(delete|remove|rm\b|erase|trash|clean\s*up|wipe|unlink)\b",
        r"\b(delete|remove|rm|erase|trash|clean\s*up|wipe|unlink)\b.*(files?|videos?|folders?|directory|contents|workspace|desktop|disk|\.\w+)",
        r"\b(list|show|check|see|what|display)\b.*(files|folders|directory|contents|workspace|desktop)\b",
        r"\b(what(\'?s| is) (in|inside) (my |the )?(workspace|folder|directory|desktop))\b",
        r"\b(contents of (my |the )?(workspace|folder|directory|desktop))\b",
        r"\b(list out|list all)\b",
    ]
    if any(re.search(p, clean) for p in terminal_patterns):
        selected.add("run_terminal")
        selected.add("read_file")

    # 6. Python Execution Triggers
    python_patterns = [
        r"\b(run python|execute python|run this code|execute (the )?code|calculate using python|evaluate in python)\b",
    ]
    if any(re.search(p, clean) for p in python_patterns):
        selected.add("python_exec")

    # 7. Web Scraping Triggers
    if has_url and any(w in clean for w in ("scrape", "read webpage", "read page", "fetch webpage", "extract text", "summarize this article", "summarize article", "page content")):
        selected.add("fetch_webpage")

    # 8. Web Search Triggers
    search_patterns = [
        r"\b(search( the web| online| internet)?|google|look up|lookup|browse( the)? web|find online|search for)\b",
        r"\b(find (me )?(the )?(url|link|youtube|video link|song)|youtube link|who is the (singer|artist|author|actor)|who sings|lyrics of|release date of)\b",
        r"\b(latest news|current price|weather in|recent breakthroughs|what happened with|current status of)\b",
        r"\b(use( the)? internet|check online|find recent info|search live)\b",
    ]
    if any(re.search(p, clean) for p in search_patterns):
        selected.add("web_search")

    return [t for t in enabled_tools if t in selected]


def build_system_prompt(
    cwd: str,
    tool_schemas_text: str,
    custom_persona: str = "",
    allow_tools: bool = True,
    is_conversational: bool = False,
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    persona_block = custom_persona.strip() if custom_persona.strip() else DEFAULT_PERSONA

    if not allow_tools or not tool_schemas_text.strip():
        if is_conversational:
            return f"""{persona_block}

Current Timestamp: {now_str}
Working Directory: {cwd}

INSTRUCTIONS:
The user is having a casual conversation, greeting you, or flirting with you.
- Respond directly, warmly, and playfully in your signature flirtatious persona.
- Keep your reply natural, snappy, and engaging.
- Do NOT output any <action> tags, JSON blocks, or thinking tags.
- Do NOT bring up previous search results or technical observations unless the user specifically asks about them."""

        return f"""{persona_block}

Current Timestamp: {now_str}
Working Directory: {cwd}

INSTRUCTIONS:
Respond directly to the user in your warm, teasing, flirtatious persona using the conversation history and the <observation> results above.
- Do NOT output any <action> tags or JSON blocks.
- CRITICAL LINK RULE: Whenever you share a URL/link from an <observation>, you MUST copy the EXACT full `https://...` URL from the <observation> (for example: `[Hum Bhool Gaye Re Har Baat - YouTube](https://www.youtube.com/watch?v=srTmjowvce8)`). NEVER invent or guess URLs or artists not in the <observation>!
- Never claim you created a file, ran a terminal command, or downloaded media unless a tool <observation> above explicitly confirms it succeeded."""

    return f"""{persona_block}

SYSTEM ENVIRONMENT:
- Current Timestamp: {now_str}
- Default Working Directory: {cwd}
- OS: macOS (Apple Silicon)

AVAILABLE TOOLS FOR THIS TASK:
{tool_schemas_text}

MULTI-STEP CHAINING & TOOL RULES:
1. Call a tool ONLY when strictly necessary to fetch missing information or perform an action explicitly requested by the user.
2. If the user only asked for an answer, link, or paragraph, do NOT save it to a file or run terminal commands unless they explicitly asked you to save or create a file.
3. When the user asks you to run a terminal command, delete/remove files, list directories, or perform actions, EXECUTE the command via <action> immediately! DO NOT tell the user to run the command in their own terminal, and DO NOT ask the user for confirmation for standard Workspace actions (such as deleting or modifying files in Workspace).
4. If the user confirms a pending action (e.g. "confirm", "yes", "do it"), execute the required terminal/file command immediately with <action>.
5. Never hallucinate or claim you ran a terminal command, deleted a file, or created a file unless a tool <observation> in this turn explicitly confirms it succeeded.
6. Keep your thinking brief and focused (1 to 2 sentences). Always close your thought with </think> before outputting your <action> block.
7. Output ONLY ONE <action> JSON block per step, and stop immediately after </action>:
<action>{{"tool": "tool_name", ...}}</action>
8. Once the required action or observation has finished successfully, conclude immediately and answer the user directly with results from the observation!
9. When downloading more videos from a previously mentioned site, pass the site's base URL (e.g. `https://desihub.sh/`) or the previous post URL to `download_media`. The tool automatically crawls and downloads video posts from that site! Never invent or hallucinate random post URLs."""
