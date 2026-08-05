/* 赛博女友 · Live2D 数字人互动
 * 连接本机 speech-to-speech 后端 (ws://localhost:8765/v1/realtime)
 * 按住 🎤 说话 → 后端 VAD 识别 → LLM 回复 → TTS 音频 → 驱动 Live2D 口型
 */
(() => {
  "use strict";

  const WS_URL = `ws://${location.hostname}:8765/v1/realtime`;
  // 可选 Live2D 形象（moc3 版本需 ≤4，Mao/Ren 版本过高不兼容当前 Cubism Core）
  // 使用旧版 Cubism Core（支持 moc3 ≤ v4）；高版本模型不可用，仅保留 4 个稳定模型
  const CHARACTERS = [
    { id: "Hiyori",  label: "Hiyori · 蓝发少女", url: "./models/Hiyori/Hiyori.model3.json" },
    { id: "Haru",    label: "Haru · 红发少女",   url: "./models/Haru/Haru.model3.json" },
    { id: "Natori",  label: "Natori · 棕发少女", url: "./models/Natori/Natori.model3.json" },
    { id: "Rice",    label: "Rice · 软萌女孩",  url: "./models/Rice/Rice.model3.json" },
  ];
  let currentCharacter = localStorage.getItem("xiaoya_character") || "Hiyori";
  function currentModelUrl() {
    return CHARACTERS.find((c) => c.id === currentCharacter)?.url || CHARACTERS[0].url;
  }
  const MIC_WORKLET = "../worklets/mic-capture.js";
  const PLAYBACK_WORKLET = "../worklets/audio-playback.js";
  const SILENCE_MS = 3500; // 松开后发送的静音时长，让后端 VAD 判定句子结束

  const $ = (id) => document.getElementById(id);
  const statusEl = $("status");
  const statusText = $("status-text");
  const chatEl = $("chat");
  const micBtn = $("mic-btn");
  const toggleVoice = $("toggle-voice");
  const voiceSelect = $("voice-select");
  const characterSelect = $("character-select");
  const langSelect = $("lang-select");
  const settingsBtn = $("settings-btn");
  const settingsModal = $("settings-modal");
  const settingsClose = $("settings-close");
  const personaInput = $("persona-input");
  const personaSave = $("persona-save");
  const evolveToggle = $("evolve-toggle");
  const personalityState = $("personality-state");
  const personalityText = $("personality-text");

  // 角色设定 & 动态性格
  // DeepSeek API 通过 7860 后端 /api/llm 代理调用（key 在后端环境变量，不落前端）
  const EVOLVE_EVERY_TURNS = 5; // 每 5 轮对话让性格演变一次
  const MEMORY_EVERY_TURNS = 8; // 每 8 轮对话提取一次长期记忆
  let memoryTurnCount = 0;
  const DEFAULT_PERSONA =
    "你是小雅，一个温柔可爱的AI女友。说话要自然、口语化，语气亲切活泼，" +
    "像真人聊天一样，不要长篇大论，每次回答控制在两三句以内。";
  let persona = localStorage.getItem("xiaoya_persona") || DEFAULT_PERSONA;
  let evolveOn = localStorage.getItem("xiaoya_evolve") !== "off";
  let personalitySummary = ""; // 动态演变出的当前性格状态
  const chatHistory = [];      // [{role:'user'|'ai', text}]
  // 长期记忆：由 DeepSeek 从对话中提取的精华信息（不受对话条数限制）
  let memoryItems = [];
  try { memoryItems = JSON.parse(localStorage.getItem("xiaoya_memory_items") || "[]"); } catch (e) { memoryItems = []; }
  let memoryHits = []; // P2 向量记忆：语义检索到的相关记忆，优先注入 instructions
  // 迁移旧 localStorage 记忆到向量记忆库（P2）
  if (memoryItems.length > 0) {
    fetch("/api/memory/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: memoryItems.slice(0, 200) }),
    }).catch(() => {});
  }
  let turnCount = 0;

  // VoiceDesign 描述式音色（由 Qwen3-TTS-12Hz-1.7B-VoiceDesign 支持，id 即文字描述 instruct）
  const QWEN_VOICES = [
    { id: "温柔清澈的年轻女声，说话自然亲切", label: "女声 · 温柔清澈 (Serena)" },
    { id: "甜美活泼的少女音，带一点俏皮", label: "女声 · 甜美活泼 (Vivian)" },
    { id: "轻柔细腻的韩系女声，发音清晰柔和", label: "女声 · 轻柔细腻 (Sohee)" },
    { id: "元气可爱的日系女声，语速轻快有活力", label: "女声 · 元气日系 (Ono Anna)" },
    { id: "低沉有磁性的成熟男声，稳重温柔", label: "男声 · 低沉磁性 (Aiden)" },
    { id: "温暖阳光的年轻男声，干净自然", label: "男声 · 温暖阳光 (Dylan)" },
    { id: "清朗爽快的男声，语速适中带亲和力", label: "男声 · 清朗爽快 (Eric)" },
    { id: "阳光少年感男声，明亮有朝气", label: "男声 · 阳光少年 (Ryan)" },
    { id: "憨厚朴实的中年男声，语调温和", label: "男声 · 憨厚朴实 (Uncle Fu)" },
  ];
  // GPT-SoVITS 自定义音色（来自 Shinsekai，本地 GPT-SoVITS API 生成）
  const GSV_BASE = ""; // 通过 7860 同源代理 /api/gsv/* 访问 GPT-SoVITS，避免跨域

  // Agent 工具（function calling）：让 AI 能联网搜索、读写文件、打开/关闭应用、打开网页
  const TOOL_DEFS = [
    {
      type: "function",
      name: "web_search",
      description:
        "Search the web for current or factual information you don't already know " +
        "(news, prices, facts, documentation). Returns the top results with titles, snippets and URLs.",
      parameters: {
        type: "object",
        properties: { query: { type: "string", description: "The search query." } },
        required: ["query"],
      },
    },
    {
      type: "function",
      name: "file",
      description:
        "Read/write/copy/move files on this computer. Supports text, PDF, Word(docx), Excel(xlsx), PowerPoint(pptx), " +
        "images (OCR), and transcribing audio files (mp3/wav/m4a/flac/ogg etc.) into text. " +
        "Actions: read/write/copy/move. For copy/move provide 'path' (source) and 'dest' (target path or folder). " +
        "Can access the whole computer's folders (writing into Windows/Program Files/ProgramData is protected). " +
        "Use for reading documents, transcribing audio, saving files, copying/moving files, or checking contents.",
      parameters: {
        type: "object",
        properties: {
          action: { type: "string", enum: ["read", "write", "copy", "move"], description: "read/write/copy/move" },
          path: { type: "string", description: "Source file path (or target for read/write)" },
          dest: { type: "string", description: "Destination path or folder (only for copy/move)" },
          content: { type: "string", description: "Content to write (only for write)" },
        },
        required: ["action", "path"],
      },
    },
    {
      type: "function",
      name: "open_app",
      description:
        "Open any application, document file, or folder on this computer. " +
        "Pass an app name (e.g. 'notepad', 'wechat', 'steam'), an absolute path to an executable (.exe), " +
        "or an absolute path to any file (a .txt document will open in the default editor) or folder.",
      parameters: {
        type: "object",
        properties: { name: { type: "string", description: "App name, or absolute path to an executable, file, or folder" } },
        required: ["name"],
      },
    },
    {
      type: "function",
      name: "browser",
      description:
        "Open a URL in the default web browser. Use when the user wants to look at a webpage.",
      parameters: {
        type: "object",
        properties: { url: { type: "string", description: "http/https URL" } },
        required: ["url"],
      },
    },
    {
      type: "function",
      name: "close_app",
      description:
        "CLOSE or QUIT an application/document on this computer. " +
        "MUST be called whenever the user asks to close/quit/exit/shut down/kill/关闭/退出/关掉/结束/杀掉 a program " +
        "(e.g. '关闭QQ', '退出微信', '把浏览器关掉'). " +
        "Pass the app name (e.g. 'QQ', 'wechat', 'chrome', 'wps') or a currently open file name (e.g. 'report.pdf').",
      parameters: {
        type: "object",
        properties: { name: { type: "string", description: "App name or open file name to close" } },
        required: ["name"],
      },
    },
    {
      type: "function",
      name: "search_computer",
      description:
        "Search this computer for files or installed programs by a name keyword. " +
        "Use when the user asks to find something on the PC, e.g. 'find my resume on the desktop', " +
        "'is there a video editor installed', 'where is the project file'.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Keyword to search for (e.g. 'resume', 'minecraft', 'report')" },
          scope: { type: "string", enum: ["all", "file", "program"], description: "all = files and programs; file = only files; program = only installed programs" },
        },
        required: ["query"],
      },
    },
    {
      type: "function",
      name: "fetch_url",
      description:
        "Fetch a web page and return its readable text content. " +
        "Use when the user wants to know what a webpage actually says, or to read the full content behind a search result link.",
      parameters: {
        type: "object",
        properties: { url: { type: "string", description: "http/https URL to fetch" } },
        required: ["url"],
      },
    },
    {
      type: "function",
      name: "grep",
      description:
        "Search the text content of files on this computer for a keyword. " +
        "Use when the user asks which file contains something, e.g. 'find which file mentions 小雅'.",
      parameters: {
        type: "object",
        properties: { query: { type: "string", description: "Keyword to find inside file contents" } },
        required: ["query"],
      },
    },
    {
      type: "function",
      name: "run_command",
      description:
        "Run a read-only diagnostic command on this computer and return its output. " +
        "Allowed commands: tasklist, netstat, ipconfig, systeminfo, ver, whoami, hostname, path, dir, ping, where, echo, vol, date, time, getmac, route, driverquery, chcp. " +
        "Use for system status questions like CPU/memory/network.",
      parameters: {
        type: "object",
        properties: { command: { type: "string", description: "The command, e.g. 'tasklist', 'ipconfig /all', 'dir C:/Users/Administrator/Desktop'" } },
        required: ["command"],
      },
    },
    {
      type: "function",
      name: "clipboard",
      description:
        "Read the system clipboard text, or write text to the clipboard. " +
        "Use when the user says 'copy this', 'paste', or 'what did I copy'.",
      parameters: {
        type: "object",
        properties: {
          action: { type: "string", enum: ["read", "write"], description: "read to get clipboard content, write to set it" },
          content: { type: "string", description: "Text to write (only needed for write)" },
        },
        required: ["action"],
      },
    },
    {
      type: "function",
      name: "system_info",
      description:
        "Get computer system status: CPU model and load, memory used/free, disk space, running process count.",
      parameters: { type: "object", properties: {} },
    },
    {
      type: "function",
      name: "docgen",
      description:
        "Generate a nicely formatted, beautiful document (Word .docx or PDF) on this computer. " +
        "Use when the user wants a well-designed document: report, notes, resume, letter, summary, 文档/报告/笔记/简历. " +
        "In content use '## ' for section headings, '- ' for list items, plain lines for paragraphs.",
      parameters: {
        type: "object",
        properties: {
          format: { type: "string", enum: ["docx", "pdf"], description: "Output format" },
          title: { type: "string", description: "Document title" },
          subtitle: { type: "string", description: "Optional subtitle" },
          content: { type: "string", description: "Content with ## headings and - lists" },
          filename: { type: "string", description: "Optional output filename (no extension)" },
        },
        required: ["title", "content"],
      },
    },
  ];
  const GSV_VOICES = [
    { id: "gsv_furina",    label: "自定义 · 芙宁娜",   gpt: "C:/Users/Administrator/shinsekai_voices/furina/models/芙宁娜_ZH-e10.ckpt",    sovits: "C:/Users/Administrator/shinsekai_voices/furina/models/芙宁娜_ZH_e10_s950_l32.pth",    ref_audio: "C:/Users/Administrator/shinsekai_voices/furina/models/【默认】根据故事走向，前往特定情景吗？听起来挺新颖。所以信封里写了什么？.wav", prompt_text: "根据故事走向，前往特定情景吗？听起来挺新颖。所以信封里写了什么？", prompt_lang: "zh" },
    { id: "gsv_aimisi",    label: "自定义 · 爱弥斯",   gpt: "C:/Users/Administrator/shinsekai_voices/Ameath2/models/aimisi-e16.ckpt",         sovits: "C:/Users/Administrator/shinsekai_voices/Ameath2/models/aimisi_e10_s180.pth",          ref_audio: "C:/Users/Administrator/shinsekai_voices/Ameath2/models/IMS.wav",                     prompt_text: "学院的效率也太低啦，现在还没发给你……", prompt_lang: "zh" },
    { id: "gsv_nanami",    label: "自定义 · 七海千秋", gpt: "C:/Users/Administrator/shinsekai_voices/nanami/models/nanami-e15.ckpt",          sovits: "C:/Users/Administrator/shinsekai_voices/nanami/models/nanami_e8_s496.pth",            ref_audio: "C:/Users/Administrator/shinsekai_voices/nanami/models/nanami.aac_0001620800_0001747840.wav", prompt_text: "でも、怪しい人の手がかりならある。", prompt_lang: "ja" },
    { id: "gsv_priestess", label: "自定义 · 普瑞赛斯", gpt: "C:/Users/Administrator/shinsekai_voices/Priestess/models/普瑞赛斯.ckpt",       sovits: "C:/Users/Administrator/shinsekai_voices/Priestess/models/普瑞赛斯.pth",             ref_audio: "C:/Users/Administrator/shinsekai_voices/Priestess/models/私の手を握って行きましょあなたは私と共に歩んでく.wav", prompt_text: "私の手を握って行きましょあなたは私と共に歩んでく", prompt_lang: "ja" },
  ];
  const VOICES = [...QWEN_VOICES, ...GSV_VOICES];
  let currentVoice = QWEN_VOICES[0].id; // 默认女声（VoiceDesign 描述）
  let lastVoice = QWEN_VOICES[0].id;    // 上一次非"自定义"的选择
  let useGPTSoVITS = false;    // 是否用 GPT-SoVITS 生成语音
  let currentGSV = null;       // 当前 GPT-SoVITS 音色配置

  // 回复语言
  const LANGS = {
    zh: { label: "中文", textLang: "zh", prompt: "请用中文回复。" },
    ja: { label: "日本語", textLang: "ja", prompt: "你必须用日语回复所有内容，忽略之前对话中使用的任何语言。从现在起所有回答都要用日语。日本語で返事してください。" },
    en: { label: "English", textLang: "en", prompt: "You MUST reply in English to everything, ignoring any language used in previous conversations. All answers must be in English now." },
    ko: { label: "한국어", textLang: "ko", prompt: "你必须用韩语回复所有内容，忽略之前对话中使用的任何语言。从现在起所有回答都要用韩语。한국어로 대답해 주세요." },
  };
  let currentLang = localStorage.getItem("xiaoya_lang") || "zh";

  // ---------- 状态 ----------
  let ws = null;
  let app = null;
  let model = null;
  let audioCtx = null;
  let micStream = null;
  let micNode = null;      // AudioWorkletNode('mic-capture')
  let playbackNode = null; // AudioWorkletNode('audio-playback')
  let analyser = null;
  let forwarding = false;  // 是否转发麦克风数据
  let listening = false;   // 是否处于"持续聆听"状态（自动 VAD）
  let voiceOn = true;      // 是否播放语音回复
  let connected = false;
  let mouth = 0;
  let userScale = 1;       // 角色缩放倍率（滚轮调节）
  let responseActive = false;    // 后端是否正在生成响应
  let pendingResponseCreate = false; // 工具结果后待触发的响应

  // ---------- 状态栏 ----------
  // 灵动岛状态栏：状态变化时展开，几秒后缩成胶囊
  let statusTimer = null;
  function setStatus(text, cls = "") {
    statusText.textContent = text;
    statusEl.className = cls; // connected/speaking/thinking/error 等，颜色由 CSS 控制
    statusEl.classList.remove("collapsed");
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => {
      statusEl.classList.add("collapsed");
    }, 3000);
  }

  // ---------- 对话气泡 ----------
  function addMsg(role, text) {
    if (!text) return;
    if (role === "ai") {
      // 小雅消息：左侧小头像角标 + 气泡
      const row = document.createElement("div");
      row.className = "msg-row ai";
      const avatar = document.createElement("div");
      avatar.className = "msg-avatar";
      avatar.textContent = "🐱";
      const bubble = document.createElement("div");
      bubble.className = "msg ai";
      bubble.textContent = text;
      row.appendChild(avatar);
      row.appendChild(bubble);
      chatEl.appendChild(row);
    } else {
      const div = document.createElement("div");
      div.className = `msg ${role}`;
      div.textContent = text;
      chatEl.appendChild(div);
    }
    chatEl.scrollTop = chatEl.scrollHeight;
    // 最多保留 30 条
    while (chatEl.children.length > 30) chatEl.removeChild(chatEl.firstChild);

    // 记录对话历史（供性格演变 + 长期记忆使用），保留最近 50 条并持久化
    chatHistory.push({ role, text });
    if (chatHistory.length > 50) chatHistory.shift();
    try {
      localStorage.setItem("xiaoya_history", JSON.stringify(chatHistory));
    } catch (e) { /* localStorage 满时忽略 */ }

    // 每次 AI 回复后累计轮次，达到阈值触发性格演变和记忆提取
    if (role === "ai") {
      // 根据回复内容判断情绪并驱动表情
      applyEmotion(detectEmotion(text));
      // P2 向量记忆：对话后异步检索相关记忆，供下轮 instructions 注入
      refreshMemoryHits();
      if (evolveOn) {
        turnCount++;
        if (turnCount >= EVOLVE_EVERY_TURNS) {
          turnCount = 0;
          maybeEvolve();
        }
      }
      memoryTurnCount++;
      if (memoryTurnCount >= MEMORY_EVERY_TURNS) {
        memoryTurnCount = 0;
        extractMemory();
      }
    }
  }

  // ---------- 角色设定 & 动态性格 & 长期记忆 ----------
  function buildInstructions() {
    const langInfo = LANGS[currentLang] || LANGS.zh;
    let instr = persona;
    if (!instr.includes(langInfo.prompt)) {
      instr += "\n" + langInfo.prompt;
    }
    // 引导小雅用 agent 工具执行真实操作，而不是只口头答应
    if (!instr.includes("【工具使用】")) {
      instr +=
        "\n【工具使用】你拥有操作这台电脑的工具能力，这是你职责的一部分。" +
        "当用户要求你打开/关闭/搜索/读写电脑上的应用、文件、网页，或查询系统状态时，" +
        "你必须【先】调用对应的工具真正执行（open_app/close_app/search_computer/grep/file/browser/system_info 等），" +
        "【再】用一两句话告诉用户结果。绝不能只口头答应、寒暄或假装做了。" +
        "重要：打开应用时【直接】调用 open_app(name=\"应用名\")，不要先 search_computer 查找位置——" +
        "open_app 会自动定位并启动。只有用户明确要\"找文件/找程序/找位置\"时才用 search_computer。" +
        "示例：用户说“打开微信”→调用 open_app(name=\"微信\")；" +
        "说“关闭QQ”→调用 close_app(name=\"QQ\")；" +
        "说“找一下合同文件”→调用 search_computer(query=\"合同\")。";
    }
    if (evolveOn && personalitySummary) {
      instr += "\n\n【当前性格状态】" + personalitySummary +
        "\n（这是根据你们聊天逐渐形成的性格，请自然地体现在你的语气和回应中）";
    }
    // 长期记忆：优先注入语义检索到的相关记忆（P2 向量记忆），兜底最近 50 条
    const memSlice = memoryHits.length ? memoryHits.slice(0, 10) : memoryItems.slice(-50);
    if (memSlice.length > 0) {
      instr += "\n\n【你长期记住的事情】\n" +
        memSlice.map((x) => "- " + x).join("\n") +
        "\n（这是你对用户长期的了解，请自然地运用在对话中，不要显得健忘）";
    }
    // 短期记忆：附加最近的对话，帮助衔接上下文
    const recent = chatHistory.slice(-6);
    if (recent.length > 0) {
      instr += "\n\n【最近对话】\n" +
        recent.map((m) => `${m.role === "user" ? "用户" : "小雅"}: ${m.text}`).join("\n") +
        "\n（请记住以上内容以衔接对话）";
    }
    return instr;
  }

  // 每 N 轮调用 DeepSeek 让角色"反思"对话，生成性格演变摘要
  async function maybeEvolve() {
    if (!evolveOn || chatHistory.length < 4) return;
    try {
      setStatus("小雅正在消化你们的对话…", "speaking");
      const messages = [
        {
          role: "system",
          content:
            "你是一个AI角色的性格管理器。根据用户的角色设定和最近的对话历史，" +
            "生成一段简短的'当前性格状态'描述（80字以内，中文），描述这个AI角色此刻的情绪、对用户的态度、" +
            "语气习惯等，要求自然、像真人慢慢产生感情一样。只输出描述本身，不要任何前缀。",
        },
        {
          role: "user",
          content:
            "角色基础设定：\n" + persona +
            "\n\n最近对话：\n" + chatHistory.slice(-10).map((m) => `${m.role}: ${m.text}`).join("\n") +
            "\n\n如果这是第1次，描述她的初始状态。之后每次让性格在上一状态基础上自然演变。",
        },
      ];
      const resp = await fetch("/api/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "deepseek-v4-flash", messages, max_tokens: 200, temperature: 0.9 }),
      });
      const data = await resp.json();
      const summary = (data?.choices?.[0]?.message?.content || "").trim();
      if (summary) {
        personalitySummary = summary;
        personalityText.textContent = summary;
        personalityState.classList.remove("hidden");
        // 把新性格应用到会话
        sendEvent({
          type: "session.update",
          session: { type: "realtime", instructions: buildInstructions() },
        });
      }
      setStatus("已连接 · 点击开始对话", "connected");
    } catch (err) {
      console.warn("性格演变失败:", err);
      setStatus("已连接 · 点击开始对话", "connected");
    }
  }

  // P2 向量记忆：用最近对话语义检索相关记忆，供 instructions 注入
  async function refreshMemoryHits() {
    try {
      const recent = chatHistory.slice(-3).map((m) => m.text).join(" ");
      const res = await fetch("/api/memory/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: recent || "最近的聊天内容", limit: 8 }),
      });
      const json = await res.json();
      memoryHits = (json.memories || []).slice(0, 8);
    } catch (e) { /* 向量记忆未就绪时静默 */ }
  }

  // 长期记忆提取：用 DeepSeek 从对话中提炼值得长期记住的精华
  async function extractMemory() {
    if (chatHistory.length < 4) return;
    try {
      const recent = chatHistory.slice(-14);
      const resp = await fetch("/api/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "deepseek-v4-flash",
          messages: [
            {
              role: "system",
              content:
                "你是记忆提取器。从用户的对话中提取值得长期记住的信息，包括：用户的喜好/厌恶、个人信息、重要事件、" +
                "用户与小雅的关系进展、用户常提的人或事。只提取重要且长期有用的，忽略寒暄和琐碎。以 JSON 数组返回，" +
                "每项形如 {\"content\": \"记忆内容\"}。没有值得记住的则返回 []。",
            },
            {
              role: "user",
              content: "对话：\n" + recent.map((m) => `${m.role}: ${m.text}`).join("\n"),
            },
          ],
          max_tokens: 300,
          temperature: 0.3,
        }),
      });
      const data = await resp.json();
      const text = (data?.choices?.[0]?.message?.content || "").trim();
      const m = text.match(/\[[\s\S]*\]/);
      if (m) {
        const items = JSON.parse(m[0]);
        let added = 0;
        if (Array.isArray(items)) {
          const newItems = [];
          for (const it of items) {
            const content = (it.content || "").trim();
            if (content && !memoryItems.some((x) => x === content)) {
              memoryItems.push(content);
              newItems.push(content);
              added++;
            }
          }
          // P2 向量记忆：新提取的记忆同步存入本地向量库
          if (newItems.length) {
            fetch("/api/memory/add", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ text: newItems }),
            }).catch(() => {});
          }
        }
        if (added > 0) {
          if (memoryItems.length > 500) memoryItems.splice(0, memoryItems.length - 500);
          localStorage.setItem("xiaoya_memory_items", JSON.stringify(memoryItems));
          // 刷新会话指令，让新记忆立即生效
          sendEvent({
            type: "session.update",
            session: { type: "realtime", instructions: buildInstructions() },
          });
          console.log(`记忆提取: +${added} 条，共 ${memoryItems.length} 条`);
        }
      }
    } catch (e) {
      console.warn("记忆提取失败:", e);
    }
  }

  // ---------- 情绪表情系统（轻量版，基于关键词 + Live2D 参数） ----------
  const EMOTIONS = {
    happy: {
      label: "开心",
      params: { ParamEyeLSmile: 1, ParamEyeRSmile: 1, ParamBrowLForm: 0.5, ParamBrowRForm: 0.5, ParamCheek: 0.3, ParamMouthForm: 0.5, ParamEyeLOpen: 0.75, ParamEyeROpen: 0.75 },
    },
    shy: {
      label: "害羞",
      params: { ParamCheek: 0.9, ParamEyeLOpen: 0.5, ParamEyeROpen: 0.5, ParamAngleZ: 3, ParamEyeBallY: -0.3 },
    },
    sad: {
      label: "难过",
      params: { ParamBrowLForm: -0.8, ParamBrowRForm: -0.8, ParamMouthForm: -0.6, ParamEyeLOpen: 0.6, ParamEyeROpen: 0.6, ParamAngleZ: -2 },
    },
    angry: {
      label: "生气",
      params: { ParamBrowLAngle: -0.8, ParamBrowRAngle: -0.8, ParamEyeLOpen: 0.9, ParamEyeROpen: 0.9, ParamMouthForm: 0.3 },
    },
    surprised: {
      label: "惊讶",
      params: { ParamEyeLOpen: 1, ParamEyeROpen: 1, ParamBrowLForm: 0.9, ParamBrowRForm: 0.9, ParamMouthForm: 0.4 },
    },
    neutral: { label: "平静", params: {} },
  };
  const EMOTION_KEYWORDS = {
    happy: ["开心", "高兴", "嘻嘻", "哈哈", "嘿嘿", "喜欢", "好呀", "棒", "兴奋", "好玩", "可爱", "笑", "甜"],
    shy: ["害羞", "脸红", "讨厌啦", "哎呀", "羞", "不好意思", "心动"],
    sad: ["难过", "伤心", "呜呜", "委屈", "唉", "寂寞", "哭", "想念", "失落"],
    angry: ["哼", "生气", "不理你", "气死", "讨厌你", "烦"],
    surprised: ["哇", "天哪", "竟然", "真的吗", "什么", "惊讶", "没想到", "居然"],
  };
  let currentEmotionParams = {};
  let currentEmotionLabel = "";

  function detectEmotion(text) {
    for (const [em, words] of Object.entries(EMOTION_KEYWORDS)) {
      if (words.some((w) => text.includes(w))) return em;
    }
    return "neutral";
  }

  function applyEmotion(emotion) {
    if (!model?.internalModel?.coreModel) return;
    try {
      const cm = model.internalModel.coreModel;
      // 恢复上一情绪的参数（口型由音量驱动，这里不碰 ParamMouthOpenY）
      for (const [k, v] of Object.entries(currentEmotionParams)) {
        cm.setParameterValueById(k, 0);
      }
      const em = EMOTIONS[emotion] || EMOTIONS.neutral;
      currentEmotionParams = em.params;
      currentEmotionLabel = em.label;
      for (const [k, v] of Object.entries(em.params)) {
        cm.setParameterValueById(k, v);
      }
    } catch (e) {
      // 某些模型缺参数时静默忽略
    }
  }

  // ---------- 工具：base64 <-> PCM ----------
  function pcm16ToB64(buffer) {
    const bytes = new Uint8Array(buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function b64ToPcm16(b64) {
    const bin = atob(b64);
    const out = new Int16Array(bin.length / 2);
    for (let i = 0, j = 0; i < bin.length; i += 2, j++) {
      out[j] = (bin.charCodeAt(i) | (bin.charCodeAt(i + 1) << 8)) << 16 >> 16;
    }
    return out;
  }

  // ---------- WebSocket ----------
  let connectFails = 0;
  let warnedBusy = false;
  function connect() {
    setStatus("连接后端…");
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      connected = true;
      connectFails = 0;
      setStatus("已连接 · 点击开始对话", "connected");
      // 配置会话（含 agent 工具）
      const session = {
        type: "realtime",
        instructions: buildInstructions(),
        audio: { output: { voice: currentVoice } },
        tools: TOOL_DEFS,
        tool_choice: "auto",
        // 关闭"说话打断"：防止扬声器回声被麦克风拾取后误判打断她的长回答
        turn_detection: { interrupt_response: false },
      };
      ws.send(JSON.stringify({ type: "session.update", session }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleMessage(msg);
    };
    ws.onclose = () => {
      connected = false;
      connectFails++;
      setStatus("连接断开，3 秒后重连…", "error");
      if (connectFails >= 3 && !warnedBusy) {
        warnedBusy = true;
        addMsg("ai", "(连接后端失败：可能服务未启动，或被另一个小雅窗口占用了连接槽位，请关闭其他小雅实例后重试)");
      }
      setTimeout(() => { if (!connected) connect(); }, 3000);
    };
    ws.onerror = () => setStatus("连接错误", "error");
  }

  function sendEvent(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  // ---------- GPT-SoVITS 自定义音色 ----------
  // 切换 GPT-SoVITS 模型和参考音频
  async function switchGSV(gsv) {
    try {
      await fetch(`${GSV_BASE}/api/gsv/set_refer_aduio?refer_audio_path=${encodeURIComponent(gsv.ref_audio)}`);
      await fetch(`${GSV_BASE}/api/gsv/set_gpt_weights?weights_path=${encodeURIComponent(gsv.gpt)}`);
      await fetch(`${GSV_BASE}/api/gsv/set_sovits_weights?weights_path=${encodeURIComponent(gsv.sovits)}`);
      return true;
    } catch (e) {
      console.warn("切换 GPT-SoVITS 音色失败:", e);
      return false;
    }
  }

  // 用 GPT-SoVITS 合成语音并播放
  async function synthGPT(text) {
    if (!currentGSV || !audioCtx) return;
    try {
      setStatus("小雅正在说话…", "speaking");
      const params = new URLSearchParams({
        text: text,
        text_lang: (LANGS[currentLang] || LANGS.zh).textLang,
        ref_audio_path: currentGSV.ref_audio,
        prompt_text: currentGSV.prompt_text,
        prompt_lang: currentGSV.prompt_lang,
        text_split_method: "cut5",
        media_type: "wav",
      });
      const resp = await fetch(`${GSV_BASE}/api/gsv/tts?${params.toString()}`);
      if (!resp.ok) { console.warn("GPT-SoVITS /tts 失败:", resp.status); return; }
      const buf = await resp.arrayBuffer();
      const audioBuf = await audioCtx.decodeAudioData(buf);
      const samples = audioBuf.getChannelData(0);
      // 重采样到 16k（audio-playback worklet 按 16k 播放）
      const ratio = 16000 / audioBuf.sampleRate;
      const out = new Float32Array(Math.max(1, Math.round(samples.length * ratio)));
      for (let i = 0; i < out.length; i++) {
        const pos = i / ratio;
        const idx = Math.floor(pos);
        const frac = pos - idx;
        const a = samples[idx];
        const b = samples[Math.min(idx + 1, samples.length - 1)];
        out[i] = a + (b - a) * frac;
      }
      if (voiceOn) {
        pushAudio(out);
      }
    } catch (e) {
      console.warn("GPT-SoVITS 合成失败:", e);
    }
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "conversation.item.input_audio_transcription.completed": {
        const t = (msg.transcript || "").trim();
        if (t) { addMsg("user", t); setStatus("思考中…", "thinking"); }
        break;
      }
      case "response.output_audio_transcript.done": {
        const t = (msg.transcript || "").trim();
        if (t) addMsg("ai", t);
        // GPT-SoVITS 模式下，用自定义音色重新合成语音
        if (useGPTSoVITS && t) synthGPT(t);
        break;
      }
      case "response.output_audio.delta": {
        // GPT-SoVITS 模式下忽略后端 Qwen3-TTS 音频，改用自定义音色
        if (!useGPTSoVITS && msg.delta) {
          const pcm = b64ToPcm16(msg.delta);
          if (voiceOn && audioCtx) {
            if (audioCtx.state !== "running") audioCtx.resume().catch(() => {});
            const samples = new Float32Array(pcm.length);
            for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
            pushAudio(samples);
          }
        }
        if (!useGPTSoVITS) setStatus("小雅正在说话…", "speaking");
        break;
      }
      case "response.audio_transcript.done": {
        const t = (msg.transcript || "").trim();
        if (t) addMsg("ai", t);
        if (useGPTSoVITS && t) synthGPT(t);
        break;
      }
      case "response.function_call_arguments.done": {
        // Agent 工具调用：执行并返回结果给模型
        runTool(msg.name, msg.arguments, msg.call_id);
        break;
      }
      case "response.created": {
        responseActive = true;
        break;
      }
      case "response.done": {
        responseActive = false;
        setStatus("已连接 · 点击开始对话", "connected");
        // 若有排队的工具结果响应，此时触发（上一响应已完成）
        if (pendingResponseCreate) {
          pendingResponseCreate = false;
          sendEvent({ type: "response.create" });
        }
        break;
      }
      case "error": {
        console.warn("server error:", msg);
        break;
      }
    }
  }

  // ---------- Agent 工具 ----------
  async function runTool(name, argsJson, callId) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch { /* keep {} */ }
    let output = "";
    if (name === "web_search") {
      const query = typeof args.query === "string" ? args.query : "";
      setStatus("正在上网查资料…", "speaking");
      output = await execWebSearch(query);
    } else if (name === "file") {
      const action = ["read", "write", "copy", "move"].includes(args.action) ? args.action : "read";
      const path = typeof args.path === "string" ? args.path : "";
      const content = typeof args.content === "string" ? args.content : "";
      const dest = typeof args.dest === "string" ? args.dest : "";
      setStatus("正在处理文件…", "speaking");
      output = await execAgentFile(action, path, content, dest);
    } else if (name === "open_app") {
      const appName = typeof args.name === "string" ? args.name : "";
      setStatus("正在打开应用…", "speaking");
      output = await execAgentOpenApp(appName);
    } else if (name === "browser") {
      const url = typeof args.url === "string" ? args.url : "";
      setStatus("正在打开网页…", "speaking");
      output = await execAgentBrowser(url);
    } else if (name === "close_app") {
      const appName = typeof args.name === "string" ? args.name : "";
      setStatus("正在关闭应用…", "speaking");
      output = await execAgentCloseApp(appName);
    } else if (name === "search_computer") {
      const query = typeof args.query === "string" ? args.query : "";
      const scope = ["file", "program"].includes(args.scope) ? args.scope : "all";
      setStatus("正在搜索本机…", "speaking");
      output = await execAgentSearch(query, scope);
    } else if (name === "fetch_url") {
      const url = typeof args.url === "string" ? args.url : "";
      setStatus("正在抓取网页…", "speaking");
      output = await execAgentFetchUrl(url);
    } else if (name === "grep") {
      const query = typeof args.query === "string" ? args.query : "";
      setStatus("正在搜索文件内容…", "speaking");
      output = await execAgentGrep(query);
    } else if (name === "run_command") {
      const command = typeof args.command === "string" ? args.command : "";
      setStatus("正在执行命令…", "speaking");
      output = await execAgentRunCommand(command);
    } else if (name === "clipboard") {
      const action = args.action === "write" ? "write" : "read";
      const content = typeof args.content === "string" ? args.content : "";
      setStatus("正在操作剪贴板…", "speaking");
      output = await execAgentClipboard(action, content);
    } else if (name === "system_info") {
      setStatus("正在读取系统信息…", "speaking");
      output = await execAgentSystemInfo();
    } else if (name === "docgen") {
      const fmt = args.format === "pdf" ? "pdf" : "docx";
      const title = typeof args.title === "string" ? args.title : "";
      const subtitle = typeof args.subtitle === "string" ? args.subtitle : "";
      const content = typeof args.content === "string" ? args.content : "";
      const filename = typeof args.filename === "string" ? args.filename : "";
      setStatus("正在生成文档…", "speaking");
      output = await execDocGen({ format: fmt, title, subtitle, content, filename });
    } else {
      output = `Unknown tool: ${name}`;
    }
    // 返回工具结果给模型
    sendEvent({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: callId, output },
    });
    // 触发模型基于结果生成回复：必须等当前响应（含工具调用的那个）完成后
    // 再发 response.create，否则会被 s2s 忽略（in_response 状态）
    if (responseActive) {
      pendingResponseCreate = true;
    } else {
      sendEvent({ type: "response.create" });
    }
  }

  async function execWebSearch(query) {
    if (!query) return "No query provided.";
    try {
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) return `搜索失败 (HTTP ${res.status})`;
      const json = await res.json();
      const today = new Date().toISOString().slice(0, 10);
      const lines = [`来自 ${today} 的网络搜索结果:`];
      if (json.answer) lines.push(`回答: ${json.answer}`);
      for (const r of json.results || []) {
        lines.push(`- ${r.title}: ${r.snippet} (${r.url})`);
      }
      return lines.length > 1 ? lines.join("\n") : `${lines[0]}\n没有找到结果。`;
    } catch (e) {
      return `搜索出错: ${e.message}`;
    }
  }

  // 文件读写/复制/移动（通过 7860 后端 /api/agent/file）
  async function execAgentFile(action, path, content, dest) {
    if (!path) return "没有提供文件路径。";
    try {
      const res = await fetch("/api/agent/file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, path, content, dest }),
      });
      const json = await res.json();
      if (!res.ok) return `文件操作失败: ${json.detail || res.status}`;
      if (action === "read") return `文件内容 (${path}):\n${json.content}`;
      if (action === "write") return `已写入 ${json.bytes} 字节到 ${path}`;
      if (action === "copy") return `已复制：${json.from} → ${json.to}`;
      if (action === "move") return `已移动：${json.from} → ${json.to}`;
      return `操作完成`;
    } catch (e) {
      return `文件操作出错: ${e.message}`;
    }
  }

  // 打开应用（通过 7860 后端 /api/agent/openapp）
  async function execAgentOpenApp(name) {
    if (!name) return "没有指定应用。";
    try {
      const res = await fetch("/api/agent/openapp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const json = await res.json();
      if (!res.ok) return `打开应用失败: ${json.detail || res.status}`;
      return `已打开 ${json.app}`;
    } catch (e) {
      return `打开应用出错: ${e.message}`;
    }
  }

  // 本机搜索（通过 7860 后端 /api/agent/search）
  async function execAgentSearch(query, scope) {
    if (!query) return "没有提供搜索关键词。";
    try {
      const res = await fetch("/api/agent/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, scope }),
      });
      const json = await res.json();
      if (!res.ok) return `搜索失败: ${json.detail || res.status}`;
      const lines = [`本机搜索结果（${json.query}）:`];
      if ((json.files || []).length) {
        lines.push("文件:");
        for (const f of json.files) lines.push(`- ${f}`);
      }
      if ((json.programs || []).length) {
        lines.push("程序:");
        for (const p of json.programs) lines.push(`- ${p}`);
      }
      if (!(json.files || []).length && !(json.programs || []).length) lines.push("没有找到。");
      return lines.join("\n");
    } catch (e) {
      return `搜索出错: ${e.message}`;
    }
  }

  // 抓取网页正文（通过 7860 后端 /api/agent/fetchurl）
  async function execAgentFetchUrl(url) {
    if (!url) return "没有提供 URL。";
    try {
      const res = await fetch("/api/agent/fetchurl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const json = await res.json();
      if (!res.ok) return `抓取失败: ${json.detail || res.status}`;
      return `网页内容 (${url}):\n${json.text}`;
    } catch (e) {
      return `抓取出错: ${e.message}`;
    }
  }

  // 文件内容搜索（通过 7860 后端 /api/agent/grep）
  async function execAgentGrep(query) {
    if (!query) return "没有提供搜索关键词。";
    try {
      const res = await fetch("/api/agent/grep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const json = await res.json();
      if (!res.ok) return `内容搜索失败: ${json.detail || res.status}`;
      const matches = json.matches || [];
      if (!matches.length) return `在文件中没有找到 "${query}"。`;
      return `内容匹配（${matches.length} 个文件）:\n` + matches.join("\n");
    } catch (e) {
      return `内容搜索出错: ${e.message}`;
    }
  }

  // 只读命令（通过 7860 后端 /api/agent/runcommand）
  async function execAgentRunCommand(command) {
    if (!command) return "没有提供命令。";
    try {
      const res = await fetch("/api/agent/runcommand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      });
      const json = await res.json();
      if (!res.ok) return `命令被拒绝: ${json.detail || res.status}`;
      return `命令输出:\n${json.output}`;
    } catch (e) {
      return `执行出错: ${e.message}`;
    }
  }

  // 剪贴板（通过 7860 后端 /api/agent/clipboard）
  async function execAgentClipboard(action, content) {
    try {
      const res = await fetch("/api/agent/clipboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, content }),
      });
      const json = await res.json();
      if (!res.ok) return `剪贴板操作失败: ${json.detail || res.status}`;
      if (action === "read") return `剪贴板内容:\n${json.content}`;
      return `已复制 ${json.bytes} 字符到剪贴板`;
    } catch (e) {
      return `剪贴板出错: ${e.message}`;
    }
  }

  // 文档生成（通过 7860 后端 /api/agent/docgen）
  async function execDocGen(args) {
    try {
      const res = await fetch("/api/agent/docgen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(args),
      });
      const json = await res.json();
      if (!res.ok) return `文档生成失败: ${json.detail || res.status}`;
      return `文档已生成：${json.path}（${json.format}）`;
    } catch (e) {
      return `文档生成出错: ${e.message}`;
    }
  }

  // 系统信息（通过 7860 后端 /api/agent/systeminfo）
  async function execAgentSystemInfo() {
    try {
      const res = await fetch("/api/agent/systeminfo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const json = await res.json();
      if (!res.ok) return `获取系统信息失败: ${json.detail || res.status}`;
      return `系统信息:\n${json.info}`;
    } catch (e) {
      return `系统信息出错: ${e.message}`;
    }
  }

  // 关闭应用（通过 7860 后端 /api/agent/closeapp）
  async function execAgentCloseApp(name) {
    if (!name) return "没有指定应用。";
    try {
      const res = await fetch("/api/agent/closeapp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const json = await res.json();
      if (!res.ok) return `关闭应用失败: ${json.detail || res.status}`;
      if (json.killed === "none-found") return `没有找到正在运行的应用「${json.app}」。`;
      return `已关闭 ${json.app}`;
    } catch (e) {
      return `关闭应用出错: ${e.message}`;
    }
  }

  // 打开网页（通过 7860 后端 /api/agent/browser）
  async function execAgentBrowser(url) {
    if (!url) return "没有提供 URL。";
    try {
      const res = await fetch("/api/agent/browser", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const json = await res.json();
      if (!res.ok) return `打开网页失败: ${json.detail || res.status}`;
      return `已打开 ${json.url}`;
    } catch (e) {
      return `打开网页出错: ${e.message}`;
    }
  }

  // ---------- 麦克风采集 ----------
  async function ensureMic() {
    if (micNode) return;
    try {
      if (!audioCtx) await ensureAudio();
      setStatus("正在请求麦克风…", "speaking");
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          channelCount: 1,
          sampleRate: 48000,
        },
      });
      const source = audioCtx.createMediaStreamSource(micStream);
      micNode = new AudioWorkletNode(audioCtx, "mic-capture");
      source.connect(micNode);
      micNode.port.onmessage = (e) => {
        if (forwarding && e.data instanceof ArrayBuffer) {
          sendEvent({ type: "input_audio_buffer.append", audio: pcm16ToB64(e.data) });
        }
      };
      setStatus("麦克风已就绪 · 开始聆听", "connected");
    } catch (err) {
      setStatus("麦克风错误: " + err.message, "error");
      console.error("麦克风初始化失败:", err);
      throw err;
    }
  }

  async function startSpeaking() {
    if (!connected || listening) return;
    try {
      await ensureMic();
      if (!audioCtx) return;
      audioCtx.resume();
      forwarding = true;
      listening = true;
      micBtn.classList.add("recording");
      setStatus("正在听你说…", "speaking");
    } catch (err) {
      setStatus("麦克风错误: " + err.message, "error");
    }
  }

  function stopSpeaking() {
    if (!listening) return;
    forwarding = false;
    listening = false;
    micBtn.classList.remove("recording");
    // 发送静音让后端 VAD 判定句子结束
    const sr = 16000;
    const chunk = new Int16Array(1280); // 40ms @16k
    const zero = new ArrayBuffer(chunk.length * 2);
    const totalChunks = Math.ceil((sr / 1000 * SILENCE_MS) / chunk.length);
    for (let i = 0; i < totalChunks; i++) {
      sendEvent({ type: "input_audio_buffer.append", audio: pcm16ToB64(zero) });
    }
    setStatus("已连接 · 点击开始对话", "connected");
  }

  // ---------- 音频输出 + 口型驱动 ----------
  function pushAudio(samples) {
    if (!samples || samples.length === 0) return;
    if (playbackNode) {
      playbackNode.port.postMessage({ kind: "audio", samples }, [samples.buffer]);
    }
  }

  async function ensureAudio() {
    if (audioCtx) return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // AudioWorklet 播放（配合 Electron 主进程音频修复，音质最好）
    try {
      await audioCtx.audioWorklet.addModule(MIC_WORKLET + "?v=20260803e");
      await audioCtx.audioWorklet.addModule(PLAYBACK_WORKLET + "?v=20260803e");
    } catch (e) {
      setStatus("音频 worklet 加载失败: " + e.message, "error");
      throw e;
    }

    playbackNode = new AudioWorkletNode(audioCtx, "audio-playback");
    playbackNode.port.postMessage({ kind: "config", inputRate: 16000 });

    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    playbackNode.connect(analyser);
    analyser.connect(audioCtx.destination);
  }

  // ---------- Live2D ----------
  let idleTimer = null;

  async function initLive2D() {
    app = new PIXI.Application({
      view: $("live2d-canvas"),
      autoStart: true,
      resizeTo: window,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      preserveDrawingBuffer: true, // 允许读取渲染帧（角色像素检测用）
      resolution: Math.min(window.devicePixelRatio || 1, 2),
    });
    model = await PIXI.live2d.Live2DModel.from(currentModelUrl(), { autoInteract: false });
    app.stage.addChild(model);
    positionModel();

    // 口型 + 呼吸 + 眨眼驱动循环（引用全局 model，切换形象后自动生效）
    const audioData = new Uint8Array(analyser ? analyser.fftSize : 256);
    let breathing = 0;
    app.ticker.add(() => {
      if (!model || !model.internalModel?.coreModel) return;
      try {
        const cm = model.internalModel.coreModel;
        // 口型：由播放音量驱动
        let target = 0;
        if (analyser) {
          analyser.getByteTimeDomainData(audioData);
          let sum = 0;
          for (let i = 0; i < audioData.length; i++) {
            const v = (audioData[i] - 128) / 128;
            sum += v * v;
          }
          target = Math.min(1, Math.sqrt(sum / audioData.length) * 8);
        }
        mouth += (target - mouth) * 0.55;
        cm.setParameterValueById("ParamMouthOpenY", mouth);

        // 呼吸：轻微起伏
        breathing += 0.03;
        cm.addParameterValueById("ParamBreath", Math.sin(breathing) * 0.02);

        // 眼睛轻微跟随鼠标
        const px = app.renderer.plugins.interaction.mouse.global.x || 0;
        cm.setParameterValueById("ParamAngleX", ((px / app.screen.width) - 0.5) * 8);
      } catch (e) {
        // 某些模型缺少参数时静默忽略
      }
    });

    startIdleTimer();
  }

  function startIdleTimer() {
    if (idleTimer) clearInterval(idleTimer);
    idleTimer = setInterval(() => {
      if (!model) return;
      const idleMotions = model.internalModel?.motionManager?.definitions?.Idle || [];
      if (!idleMotions.length) return;
      const idx = Math.floor(Math.random() * idleMotions.length);
      model.motion("Idle", idx).catch(() => {});
    }, 25000);
  }

  // 调试钩子（供外部检查模型状态）
  window.__xiaoyaDebug = {
    getModel: () => {
      if (!model) return null;
      try {
        const b = model.getBounds();
        return { width: model.width, height: model.height, scaleX: model.scale.x, boundsW: b.width, boundsH: b.height, x: model.x, y: model.y };
      } catch (e) {
        return { width: model.width, height: model.height, scaleX: model.scale.x, err: e.message };
      }
    },
    getEmotion: () => ({ label: currentEmotionLabel, params: currentEmotionParams }),
    applyEmotion: (emotion) => applyEmotion(emotion),
    detectEmotion: (text) => detectEmotion(text),
    getCharBounds: () => charPixelBounds,
    ensureMic: () => ensureMic(),
  };

  // 切换 Live2D 形象
  async function switchModel(url) {
    if (!app) return;
    if (model) {
      app.stage.removeChild(model);
      model.destroy({ children: true });
      model = null;
    }
    model = await PIXI.live2d.Live2DModel.from(url, { autoInteract: false });
    app.stage.addChild(model);
    positionModel();
    startIdleTimer();
  }

  function positionModel() {
    if (!model || !app) return;
    model.anchor.set(0.5, 1);
    model.x = app.screen.width / 2;
    model.y = app.screen.height - 20;
    model.scale.set(1);
    // 等一帧后用模型实际渲染边界缩放，保证不同模型都适配画布
    requestAnimationFrame(() => {
      if (!model) return;
      let w, h;
      try {
        const b = model.getBounds();
        w = b.width || model.width || 2048;
        h = b.height || model.height || 2048;
      } catch (e) {
        w = model.width || 2048;
        h = model.height || 2048;
      }
      const scale = Math.min(app.screen.width / w, app.screen.height / h) * 0.9 * userScale;
      model.scale.set(scale);
      scheduleCharBoundsRefresh();
    });
  }

  // ---------- 像素级角色拖拽（仅角色实体可拖动窗口，空白不拖） ----------
  let charPixelBounds = null;   // 角色不透明像素的包围盒（画布坐标）
  let refreshTimer = null;

  // 从 WebGL 画布读取像素，找出角色实际占据的区域
  function getCharacterPixelBounds() {
    if (!app) return null;
    try {
      const { gl } = app.renderer;
      const bw = gl.drawingBufferWidth || app.screen.width;
      const bh = gl.drawingBufferHeight || app.screen.height;
      const res = app.renderer.resolution || 1;
      const pixels = new Uint8Array(bw * bh * 4);
      gl.readPixels(0, 0, bw, bh, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      // 降采样（步进 2）提升速度，alpha 阈值过滤透明区域
      for (let y = 0; y < bh; y += 2) {
        for (let x = 0; x < bw; x += 2) {
          if (pixels[(y * bw + x) * 4 + 3] > 25) {
            const cy = bh - 1 - y; // WebGL 像素 y 轴翻转
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (cy < minY) minY = cy;
            if (cy > maxY) maxY = cy;
          }
        }
      }
      if (minX === Infinity) return null;
      // 物理像素 → 逻辑坐标（与鼠标 clientX/Y 对齐）
      return { minX: minX / res, minY: minY / res, maxX: maxX / res, maxY: maxY / res };
    } catch (e) {
      return null;
    }
  }

  // 角色位置/大小变化后延迟刷新像素边界（等渲染完成），并同步拖拽层位置
  function scheduleCharBoundsRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      charPixelBounds = getCharacterPixelBounds();
      updateDragZone();
    }, 150);
  }

  // 把像素边界同步到 #drag-zone（系统原生拖拽层，仅覆盖角色像素区域）
  function updateDragZone() {
    const zone = $("drag-zone");
    if (!zone || !charPixelBounds) return;
    zone.style.left = charPixelBounds.minX + "px";
    zone.style.top = charPixelBounds.minY + "px";
    zone.style.width = (charPixelBounds.maxX - charPixelBounds.minX) + "px";
    zone.style.height = (charPixelBounds.maxY - charPixelBounds.minY) + "px";
  }

  // ---------- 事件绑定 ----------
  // 防止长音频播放中 AudioContext 被系统挂起导致"念一半停"
  window.addEventListener("visibilitychange", () => {
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  });
  setInterval(() => {
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  }, 4000);

  micBtn.addEventListener("click", () => {
    if (listening) stopSpeaking();
    else startSpeaking();
  });

  // 顶部选项面板开关
  $("toolbar-btn").addEventListener("click", () => {
    $("toolbar-panel").classList.toggle("hidden");
  });

  // 文字输入发送
  function sendTextMessage() {
    const input = $("text-input");
    const text = input.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addMsg("ai", "(连接未就绪，稍后再试)");
      return;
    }
    if (listening) stopSpeaking(); // 停止麦克风聆听，避免冲突
    addMsg("user", text);
    sendEvent({
      type: "conversation.item.create",
      item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
    });
    sendEvent({ type: "response.create" });
    input.value = "";
    setStatus("思考中…", "thinking");
  }
  $("send-btn").addEventListener("click", sendTextMessage);
  $("text-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendTextMessage();
  });

  // ---------- Emoji 表情 ----------
  const emojiBtn = $("emoji-btn");
  const emojiPanel = $("emoji-panel");
  emojiBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    emojiPanel.classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!emojiPanel.classList.contains("hidden") &&
        !e.target.closest("#emoji-panel") && !e.target.closest("#emoji-btn")) {
      emojiPanel.classList.add("hidden");
    }
  });
  // 分类标签页切换（QQ/微信式）
  emojiPanel.querySelectorAll(".emoji-tab").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      emojiPanel.querySelectorAll(".emoji-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const cat = tab.dataset.cat;
      emojiPanel.querySelectorAll(".emoji-grid").forEach((g) => {
        g.classList.toggle("active", g.dataset.catBody === cat);
      });
    });
  });
  emojiPanel.querySelectorAll(".emoji-item").forEach((b) => {
    b.addEventListener("click", () => {
      const input = $("text-input");
      const start = input.selectionStart ?? input.value.length;
      const end = input.selectionEnd ?? input.value.length;
      input.value = input.value.slice(0, start) + b.textContent + input.value.slice(end);
      input.selectionStart = input.selectionEnd = start + b.textContent.length;
      input.focus();
    });
  });

  // ---------- 文件上传（传文档/录音/图片给小雅解析）----------
  const uploadBtn = $("upload-btn");
  const fileInput = $("file-input");
  uploadBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const files = fileInput.files;
    if (!files || !files.length) return;
    for (const f of Array.from(files)) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        setStatus("正在上传文件…", "speaking");
        const res = await fetch("/api/upload", { method: "POST", body: fd });
        const json = await res.json();
        if (!res.ok) { addMsg("ai", `(上传失败: ${json.detail || res.status})`); continue; }
        addMsg("user", `📎 我上传了文件：${json.filename}`);
        sendEvent({
          type: "conversation.item.create",
          item: { type: "message", role: "user", content: [{ type: "input_text", text: `我上传了一个文件，请帮我读取并解析它的内容：文件路径是 ${json.path}` }] },
        });
        sendEvent({ type: "response.create" });
        setStatus("正在解析你上传的文件…", "thinking");
      } catch (e) {
        addMsg("ai", `(上传出错: ${e.message})`);
      }
    }
    fileInput.value = "";
  });

  toggleVoice.addEventListener("click", () => {
    voiceOn = !voiceOn;
    toggleVoice.textContent = voiceOn ? "🔊 语音回复" : "🔇 静音回复";
  });

  // 音色选择器（分组：内置音色 / GPT-SoVITS 自定义音色）
  function addVoiceGroup(label, list) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const v of list) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.label;
      if (v.id === currentVoice) opt.selected = true;
      group.appendChild(opt);
    }
    voiceSelect.appendChild(group);
  }
  addVoiceGroup("内置音色", QWEN_VOICES);
  addVoiceGroup("GPT-SoVITS 自定义", GSV_VOICES);
  // 自定义音色描述（VoiceDesign：输入任意文字描述生成专属音色）
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "✏️ 自定义音色描述…";
  voiceSelect.appendChild(customOpt);

  // 形象选择器
  for (const c of CHARACTERS) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.label;
    if (c.id === currentCharacter) opt.selected = true;
    characterSelect.appendChild(opt);
  }
  characterSelect.addEventListener("change", async () => {
    currentCharacter = characterSelect.value;
    localStorage.setItem("xiaoya_character", currentCharacter);
    try {
      await switchModel(currentModelUrl());
      addMsg("ai", `(已切换形象为 ${CHARACTERS.find((c) => c.id === currentCharacter)?.label})`);
    } catch (err) {
      console.warn("切换形象失败:", err);
      addMsg("ai", "(切换形象失败)");
    }
  });

  // 语言选择器
  langSelect.value = currentLang;
  langSelect.addEventListener("change", () => {
    currentLang = langSelect.value;
    localStorage.setItem("xiaoya_lang", currentLang);
    // 更新会话指令，让 AI 用所选语言回复
    sendEvent({
      type: "session.update",
      session: { type: "realtime", instructions: buildInstructions() },
    });
    addMsg("ai", `(回复语言已切换为 ${(LANGS[currentLang] || LANGS.zh).label})`);
  });

  // 应用音色切换（发 session.update 或切 GPT-SoVITS）
  function applyVoiceChange(sel) {
    currentVoice = sel;
    lastVoice = sel;
    const label = VOICES.find((v) => v.id === sel)?.label || sel;
    if (sel.startsWith("gsv_")) {
      // 切到 GPT-SoVITS 自定义音色
      const gsv = GSV_VOICES.find((v) => v.id === sel);
      if (!gsv) return;
      useGPTSoVITS = true;
      currentGSV = gsv;
      switchGSV(gsv).then((ok) => {
        addMsg("ai", ok ? `(已切换声音为 ${label})` : `(切换 ${label} 失败，请确认 GPT-SoVITS 已启动)`);
      });
    } else {
      // 切回内置 Qwen3-TTS 音色（VoiceDesign 描述式）
      useGPTSoVITS = false;
      currentGSV = null;
      sendEvent({
        type: "session.update",
        session: { type: "realtime", audio: { output: { voice: sel } } },
      });
      addMsg("ai", `(已切换声音为 ${label})`);
    }
  }

  // ---------- 自定义音色管理（持久化到 localStorage，含名字标签）----------
  let customVoices = [];  // [{name, desc}]
  try {
    const raw = JSON.parse(localStorage.getItem("xiaoya_custom_voices") || "[]") || [];
    // 兼容旧数据（纯字符串 desc）→ 迁移为 {name, desc}
    customVoices = raw.map((x) => (typeof x === "string" ? { name: x.slice(0, 8), desc: x } : x));
  } catch (e) {}
  let editingCustomIndex = -1;

  function saveCustomVoices() {
    try { localStorage.setItem("xiaoya_custom_voices", JSON.stringify(customVoices)); } catch (e) {}
  }

  function customOptionLabel(cv) {
    return "✏️ " + (cv.name || cv.desc.slice(0, 8));
  }

  // 重建下拉里的自定义音色 option（显示名字，value 仍用描述发给后端）
  function rebuildCustomOptions() {
    voiceSelect.querySelectorAll("option[data-custom]").forEach((o) => o.remove());
    for (const cv of customVoices) {
      const opt = document.createElement("option");
      opt.value = cv.desc;
      opt.textContent = customOptionLabel(cv);
      opt.dataset.custom = "1";
      voiceSelect.insertBefore(opt, voiceSelect.querySelector('option[value="__custom__"]'));
    }
  }

  // 渲染管理弹窗里的音色列表（名字 + 描述）
  function renderCustomVoiceList() {
    const listEl = $("voice-custom-list");
    const emptyEl = $("voice-custom-empty");
    listEl.innerHTML = "";
    if (customVoices.length === 0) { emptyEl.style.display = "block"; return; }
    emptyEl.style.display = "none";
    customVoices.forEach((cv, i) => {
      const item = document.createElement("div");
      item.className = "memory-item";
      item.style.flexDirection = "column";
      item.style.alignItems = "stretch";
      const head = document.createElement("div");
      head.style.display = "flex";
      head.style.alignItems = "center";
      const nameEl = document.createElement("b");
      nameEl.textContent = "✏️ " + (cv.name || cv.desc.slice(0, 8));
      nameEl.style.flex = "1";
      nameEl.style.fontSize = "13px";
      const btns = document.createElement("span");
      btns.style.flexShrink = "0";
      const edit = document.createElement("button");
      edit.className = "mi-del"; edit.title = "修改"; edit.textContent = "✏️";
      edit.onclick = () => {
        editingCustomIndex = i;
        $("voice-custom-name").value = cv.name || "";
        $("voice-custom-input").value = cv.desc;
        $("voice-custom-apply").textContent = "更新音色";
        $("voice-custom-cancel-edit").style.display = "inline-block";
        $("voice-custom-name").focus();
      };
      const del = document.createElement("button");
      del.className = "mi-del"; del.title = "删除"; del.textContent = "🗑";
      del.onclick = () => {
        customVoices.splice(i, 1);
        saveCustomVoices();
        rebuildCustomOptions();
        renderCustomVoiceList();
        if (currentVoice === cv.desc) {  // 删的是当前音色 → 恢复默认
          voiceSelect.value = QWEN_VOICES[0].id;
          applyVoiceChange(QWEN_VOICES[0].id);
        }
      };
      btns.appendChild(edit);
      btns.appendChild(del);
      head.appendChild(nameEl);
      head.appendChild(btns);
      const descEl = document.createElement("div");
      descEl.textContent = cv.desc;
      descEl.style.fontSize = "11px";
      descEl.style.color = "#94a3b8";
      descEl.style.marginTop = "2px";
      descEl.style.overflow = "hidden";
      descEl.style.textOverflow = "ellipsis";
      descEl.style.whiteSpace = "nowrap";
      item.appendChild(head);
      item.appendChild(descEl);
      listEl.appendChild(item);
    });
  }

  function openCustomVoiceModal() {
    editingCustomIndex = -1;
    $("voice-custom-name").value = "";
    $("voice-custom-input").value = "";
    $("voice-custom-apply").textContent = "保存音色";
    $("voice-custom-cancel-edit").style.display = "none";
    renderCustomVoiceList();
    $("voice-custom-modal").classList.remove("hidden");
    $("voice-custom-name").focus();
  }

  // 保存（新增或更新）自定义音色：名字 + 描述
  function saveCustomVoice() {
    const desc = $("voice-custom-input").value.trim();
    if (!desc) return;
    const name = $("voice-custom-name").value.trim() || desc.slice(0, 8);
    if (editingCustomIndex >= 0) {
      const old = customVoices[editingCustomIndex].desc;
      customVoices[editingCustomIndex] = { name, desc };
      if (currentVoice === old) { currentVoice = desc; lastVoice = desc; }
      editingCustomIndex = -1;
    } else {
      const exist = customVoices.find((cv) => cv.desc === desc);
      if (exist) exist.name = name;
      else customVoices.push({ name, desc });
    }
    saveCustomVoices();
    rebuildCustomOptions();
    renderCustomVoiceList();
    $("voice-custom-name").value = "";
    $("voice-custom-input").value = "";
    $("voice-custom-apply").textContent = "保存音色";
    $("voice-custom-cancel-edit").style.display = "none";
    const opt = voiceSelect.querySelector(`option[value="${CSS.escape(desc)}"]`);
    if (opt) opt.selected = true;
    applyVoiceChange(desc);
  }

  voiceSelect.addEventListener("change", async () => {
    const sel = voiceSelect.value;
    if (sel === "__custom__") {
      // 打开自定义音色管理弹窗（Electron 不支持 window.prompt）
      voiceSelect.value = lastVoice;
      openCustomVoiceModal();
      return;
    }
    applyVoiceChange(sel);
  });

  // 自定义音色弹窗事件：保存 / 关闭 / 取消编辑 / 回车
  $("voice-custom-apply").addEventListener("click", saveCustomVoice);
  $("voice-custom-close").addEventListener("click", () => $("voice-custom-modal").classList.add("hidden"));
  $("voice-custom-cancel-edit").addEventListener("click", () => {
    editingCustomIndex = -1;
    $("voice-custom-name").value = "";
    $("voice-custom-input").value = "";
    $("voice-custom-apply").textContent = "保存音色";
    $("voice-custom-cancel-edit").style.display = "none";
  });
  $("voice-custom-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveCustomVoice();
  });
  $("voice-custom-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveCustomVoice();
  });
  rebuildCustomOptions();

  window.addEventListener("resize", positionModel);

  // ---------- 角色设定弹窗 ----------
  const scaleSlider = $("scale-slider");
  const scaleValue = $("scale-value");
  const winSlider = $("win-slider");
  const winValue = $("win-value");
  const winSizeRow = $("win-size-row");
  const PET_BASE_W = 620, PET_BASE_H = 880;

  // 桌宠模式才显示"窗口大小"调节（需要 Electron 的 window.pet.resize）
  if (window.pet) winSizeRow.style.display = "flex";

  function setPetCloseVisible(show) {
    const pc = $("pet-close");
    if (pc) pc.style.display = show ? "" : "none";
  }

  settingsBtn.addEventListener("click", () => {
    setPetCloseVisible(false); // 打开设定时隐藏桌宠关闭键，避免视觉重合
    personaInput.value = persona;
    evolveToggle.checked = evolveOn;
    scaleSlider.value = Math.round(userScale * 100);
    scaleValue.textContent = Math.round(userScale * 100) + "%";
    if (window.pet) {
      const pct = Math.round(((window.outerWidth || PET_BASE_W) / PET_BASE_W) * 100);
      winSlider.value = Math.min(200, Math.max(50, pct));
      winValue.textContent = winSlider.value + "%";
    }
    if (personalitySummary) {
      personalityText.textContent = personalitySummary;
      personalityState.classList.remove("hidden");
    } else {
      personalityState.classList.add("hidden");
    }
    refreshMemoryList();
    settingsModal.classList.remove("hidden");
  });

  // 长期记忆管理
  // 打开弹窗时从向量库拉取全部记忆（P2），后端不可用则用本地 localStorage 兜底
  async function refreshMemoryList() {
    try {
      const res = await fetch("/api/memory/list");
      const json = await res.json();
      if (res.ok && Array.isArray(json.memories)) {
        memoryItems = json.memories.slice(-500);
        localStorage.setItem("xiaoya_memory_items", JSON.stringify(memoryItems));
      }
    } catch (e) { /* 后端未就绪，用本地 */ }
    renderMemoryList();
  }
  function renderMemoryList() {
    const list = $("memory-list");
    const count = $("memory-count");
    if (!list) return;
    count.textContent = memoryItems.length + " 条";
    list.innerHTML = "";
    if (memoryItems.length === 0) {
      list.innerHTML = '<div class="memory-empty">还没有记忆，多聊几句她会记住你的事</div>';
      return;
    }
    memoryItems.forEach((mem, idx) => {
      const div = document.createElement("div");
      div.className = "memory-item";
      const del = document.createElement("button");
      del.className = "mi-del";
      del.textContent = "×";
      del.title = "删除这条记忆";
      del.addEventListener("click", () => {
        const txt = mem;
        memoryItems.splice(idx, 1);
        localStorage.setItem("xiaoya_memory_items", JSON.stringify(memoryItems));
        // P2：同步删除向量库中的这条记忆
        fetch("/api/memory/del", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: txt }),
        }).catch(() => {});
        renderMemoryList();
        sendEvent({ type: "session.update", session: { type: "realtime", instructions: buildInstructions() } });
      });
      const span = document.createElement("span");
      span.textContent = mem;
      div.appendChild(del);
      div.appendChild(span);
      list.appendChild(div);
    });
  }

  const memoryClearBtn = $("memory-clear");
  if (memoryClearBtn) {
    memoryClearBtn.addEventListener("click", () => {
      if (!memoryItems.length) return;
      memoryItems = [];
      localStorage.setItem("xiaoya_memory_items", JSON.stringify(memoryItems));
      // P2：同步清空向量库
      fetch("/api/memory/clear", { method: "POST" }).catch(() => {});
      renderMemoryList();
      sendEvent({ type: "session.update", session: { type: "realtime", instructions: buildInstructions() } });
      addMsg("ai", "(已清空长期记忆)");
    });
  }
  // 角色大小：拖动时只更新显示，松手（change）才应用，避免缩放过程中滑块跳动
  scaleSlider.addEventListener("input", () => {
    scaleValue.textContent = scaleSlider.value + "%";
  });
  scaleSlider.addEventListener("change", () => {
    userScale = scaleSlider.value / 100;
    scaleValue.textContent = scaleSlider.value + "%";
    positionModel();
  });
  // 窗口大小：拖动时只更新显示，松手（change）才真正调整，避免窗口实时变化导致滑块乱动
  winSlider.addEventListener("input", () => {
    winValue.textContent = winSlider.value + "%";
  });
  winSlider.addEventListener("change", () => {
    if (!window.pet) return;
    const pct = winSlider.value / 100;
    window.pet.resize(Math.round(PET_BASE_W * pct), Math.round(PET_BASE_H * pct));
  });
  settingsClose.addEventListener("click", () => { settingsModal.classList.add("hidden"); setPetCloseVisible(true); });
  settingsModal.addEventListener("click", (e) => {
    if (e.target === settingsModal) { settingsModal.classList.add("hidden"); setPetCloseVisible(true); }
  });
  personaSave.addEventListener("click", () => {
    persona = personaInput.value.trim() || DEFAULT_PERSONA;
    evolveOn = evolveToggle.checked;
    localStorage.setItem("xiaoya_persona", persona);
    localStorage.setItem("xiaoya_evolve", evolveOn ? "on" : "off");
    // 立即应用新设定
    sendEvent({
      type: "session.update",
      session: { type: "realtime", instructions: buildInstructions() },
    });
    settingsModal.classList.add("hidden");
    setPetCloseVisible(true);
    addMsg("ai", "(角色设定已更新)");
  });

  // ---------- 启动 ----------
  (async () => {
    try {
      // 恢复长期记忆（跨会话保留的对话历史）
      try {
        const saved = JSON.parse(localStorage.getItem("xiaoya_history") || "[]");
        if (Array.isArray(saved)) {
          for (const m of saved) {
            if (m && typeof m.text === "string" && (m.role === "user" || m.role === "ai")) {
              chatHistory.push({ role: m.role, text: m.text });
              const div = document.createElement("div");
              div.className = `msg ${m.role}`;
              div.textContent = m.text;
              chatEl.appendChild(div);
            }
          }
          chatEl.scrollTop = chatEl.scrollHeight;
        }
      } catch (e) { /* 记忆恢复失败不影响使用 */ }

      await ensureAudio();
      await initLive2D();
      connect();
    } catch (err) {
      console.error(err);
      setStatus("初始化失败: " + err.message, "error");
    }
  })();
})();
