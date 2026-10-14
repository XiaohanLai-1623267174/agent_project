<template>
  <div class="app-shell">
    <!-- ======== Sidebar ======== -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="sidebar-header">
        <button class="btn-icon" title="新对话" @click="newChat">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </button>
        <span v-if="!sidebarCollapsed" class="sidebar-title">对话历史</span>
        <button class="btn-icon" :title="sidebarCollapsed ? '展开' : '收起'" @click="sidebarCollapsed = !sidebarCollapsed">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <polyline v-if="sidebarCollapsed" points="9,18 15,12 9,6"/>
            <polyline v-else points="15,18 9,12 15,6"/>
          </svg>
        </button>
      </div>

      <div v-if="!sidebarCollapsed" class="sidebar-list">
        <!-- 搜索：对话多于一条时才显示 -->
        <div v-if="conversations.length > 1" class="conv-search">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>
          </svg>
          <input
            v-model="convQuery"
            class="conv-search-input"
            type="text"
            placeholder="搜索对话"
            @keydown.esc="convQuery = ''"
          />
          <button v-if="convQuery" class="conv-search-clear" title="清空搜索" @click="convQuery = ''">×</button>
        </div>

        <div
          v-for="conv in filteredConversations"
          :key="conv.id"
          class="chat-item"
          :class="{ active: conv.id === activeConvId }"
          @click="onConvClick(conv.id)"
        >
          <!-- 重命名：双击标题进入编辑；Enter 保存、Esc 取消、点走也保存 -->
          <input
            v-if="renamingId === conv.id"
            v-model="renameText"
            class="chat-item-rename"
            type="text"
            maxlength="60"
            @click.stop
            @keydown.enter.prevent="commitRename(conv.id)"
            @keydown.esc.prevent="cancelRename"
            @blur="commitRename(conv.id)"
          />
          <span
            v-else
            class="chat-item-title"
            :title="conv.title"
            @dblclick.stop="startRename(conv)"
          >{{ conv.title }}</span>

          <!-- 删除：二次确认 -->
          <template v-if="confirmDeleteId === conv.id">
            <button class="btn-confirm" title="确认删除这段对话" @click.stop="deleteChat(conv.id)">删除</button>
            <button class="btn-confirm ghost" title="取消" @click.stop="cancelDelete">取消</button>
          </template>
          <button v-else class="btn-delete" title="删除" @click.stop="askDelete(conv.id)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <polyline points="3,6 5,6 21,6"/><path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6M8,6V4a2,2,0,0,1,2-2h4a2,2,0,0,1,2,2V6"/>
            </svg>
          </button>
        </div>

        <p v-if="conversations.length === 0" class="sidebar-empty">暂无对话记录</p>
        <p v-else-if="filteredConversations.length === 0" class="sidebar-empty">没有匹配「{{ convQuery }}」的对话</p>
      </div>
    </aside>

    <!-- ======== Main Chat Area ======== -->
    <main class="chat-main">
      <!-- Header -->
      <header class="chat-header">
        <div class="header-left">
          <button v-if="sidebarCollapsed" class="btn-icon" title="展开侧栏" @click="sidebarCollapsed = false">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>
            </svg>
          </button>
          <span class="header-title">AI 知识库助手</span>
        </div>
        <div class="header-right">
          <button v-if="messages.length > 0" class="btn-text" @click="newChat">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            新对话
          </button>
          <span class="user-tag">{{ auth.state.username }}</span>
          <button class="btn-text" @click="handleLogout">退出</button>
        </div>
      </header>

      <!-- Messages -->
      <!-- ★ P1-1 v2：跟随与否由【用户意图】决定 —— `@wheel/@touch*` 捕捉"用户想看上文"，
           程序自己的滚动靠 `progTarget` 排除，不再拿"距底多少像素"当停止的判据 -->
      <div ref="messagesContainer" class="messages-area" @scroll.passive="onMessagesScroll"
           @wheel.passive="onWheel" @touchstart.passive="onTouchStart"
           @touchmove.passive="onTouchMove" @keydown="onMessagesKeydown">
        <!-- Welcome -->
        <div v-if="messages.length === 0 && !isWaiting" class="welcome">
          <div class="welcome-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              <path d="M8 9h8" opacity="0.5"/><path d="M8 13h5" opacity="0.3"/>
            </svg>
          </div>
          <h1 class="welcome-heading">你好，我是 AI 知识库助手</h1>
          <p class="welcome-sub">专注解答奖学金申请、综测评定相关问题</p>
          <div class="welcome-hints">
            <button
              v-for="hint in welcomeHints"
              :key="hint"
              class="hint-chip"
              @click="sendHint(hint)"
            >{{ hint }}</button>
          </div>
        </div>

        <!-- Message bubbles -->
        <div
          v-for="(msg, index) in messages"
          :key="index"
          class="message-row"
          :class="msg.role"
        >
          <div class="msg-avatar">
            <template v-if="msg.role === 'user'">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>
              </svg>
            </template>
            <template v-else>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2a4 4 0 0 1 4 4v1h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h2V6a4 4 0 0 1 4-4z"/>
                <circle cx="12" cy="13" r="2" opacity="0.6"/>
              </svg>
            </template>
          </div>

          <div class="msg-body">
            <div class="msg-bubble">
              <template v-if="msg.streaming">
                <div class="markdown-body" v-html="renderMarkdown(msg.content, msg.sources)" @click="onCiteClick($event, index)"></div>
                <span class="typing-cursor">|</span>
              </template>
              <template v-else>
                <div class="markdown-body" v-html="renderMarkdown(msg.content, msg.sources)" @click="onCiteClick($event, index)"></div>
              </template>
            </div>

            <!-- 检索来源（引用溯源）：正文里的 [n] 角标与这里序号一一对应 -->
            <div v-if="!msg.streaming && msg.role === 'assistant' && msg.sources && msg.sources.length" class="msg-sources">
              <button class="sources-toggle" @click="toggleSources(index)">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
                <span>来源 {{ msg.sources.length }} 段</span>
                <span v-if="msg.retrieval && msg.retrieval.candidates" class="sources-cand">
                  检索到 {{ msg.retrieval.candidates }} 段
                </span>
                <!-- 重排降级提示：后端重排不可用时会回退成原始召回顺序，
                     必须让用户知道来源顺序未经重排，而不是当作正常结果。 -->
                <span v-if="msg.retrieval && msg.retrieval.reranked === false"
                      class="sources-warn"
                      :title="msg.retrieval.degradedReason || '重排服务不可用'">
                  未重排
                </span>
                <svg class="src-chevron" :class="{ open: openSources[index] }" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                  <polyline points="6,9 12,15 18,9"/>
                </svg>
              </button>
              <ol v-show="openSources[index]" class="sources-list">
                <li v-for="s in msg.sources" :key="s.n"
                    :class="{ hit: activeCite === index + ':' + s.n }">
                  <span class="src-n">{{ s.n }}</span>
                  <div class="src-main">
                    <div class="src-head">
                      <span class="src-file">{{ s.filename || s.doc_title || ('文档 #' + s.document_id) }}</span>
                      <span v-if="s.score !== null && s.score !== undefined" class="src-score">{{ Number(s.score).toFixed(3) }}</span>
                    </div>
                    <div v-if="s.section_path" class="src-path">{{ s.section_path }}</div>
                    <div v-if="s.snippet" class="src-snippet">{{ s.snippet }}</div>
                  </div>
                </li>
              </ol>
            </div>
            <div v-else-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval && msg.retrieval.attempted && !msg.retrieval.candidates"
                 class="msg-sources is-empty">未检索到相关资料</div>

            <!-- ★ 「本条压根没走检索」—— 后端在流结束时补发的状态帧（attempted:false）。
                 没有这个提示，"没检索"与"没数据"在界面上长得一模一样，
                 用户就无法分辨这条答案是查了资料、还是模型自己答的（§39.25 / §39.27）。
                 ⚠️ 必须自成一条链、并放在上面那条 v-if/v-else-if 链【之外】：
                    往链里插兄弟节点会截断它、把「未检索到相关资料」顶掉（§39.24.3）。 -->
            <div v-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval && msg.retrieval.attempted === false"
                 class="msg-sources is-empty">本条未检索资料（判断为无需查库）</div>

            <!-- ★ 联网搜索（§39.38）：**自成一条链**，放在上面那两条之外
                   （往 v-if/v-else-if 链里插兄弟节点会截断它，§39.24.3 踩过）。
                 三条诚实规则直接落在界面上：
                   ① 标题就写「不是学校文件」——与上面的「来源 N 段」（学校文件）视觉、措辞都分开；
                   ② 摘要**不编号**：编号只属于学校文件（引用角标那套只认 msg.sources）；
                   ③ 搜不到时**必须显示"没搜到"**，绝不静默留白（静默＝用户以为搜过了）。 -->
            <div v-if="!msg.streaming && msg.role === 'assistant' && msg.web && msg.web.attempted"
                 class="msg-web">
              <div v-if="msg.web.count > 0" class="web-head">
                <span class="web-badge">🌐 网页参考 {{ msg.web.count }} 条</span>
                <span class="web-note">来自公开网页，仅供参考，<b>不是学校文件</b></span>
              </div>
              <ol v-if="msg.web.count > 0" class="web-list">
                <li v-for="(w, wi) in msg.web.items" :key="wi">
                  <a class="web-title" :href="w.url" target="_blank" rel="noopener noreferrer"
                     :title="w.url">{{ w.title }}</a>
                  <span class="web-site">{{ w.site }}</span>
                  <div v-if="w.snippet" class="web-snippet">{{ w.snippet }}</div>
                </li>
              </ol>
              <!-- 搜不到 / 搜失败：如实说明原因，而不是什么都不显示 -->
              <div v-else class="web-empty">
                本次联网搜索没搜到可用结果{{ msg.web.reason_text ? '（' + msg.web.reason_text + '）' : '' }}
              </div>
            </div>

            <!-- ★ 检索详情（调试/对照用）：把流水线四步摊开 —— 路由 → 召回 → 重排 → 引用。
                 数据全部来自同一个 sources 帧（未改任何后端代码）。
                 ⚠️ 这段必须放在上面那条 v-if/v-else-if 链【之外】，
                    否则会截断链、把"未检索到相关资料"顶掉。 -->
            <div v-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval && msg.retrieval.attempted" class="msg-debug">
              <button class="debug-toggle" @click="toggleDebug(index)">
                <span>检索详情</span>
                <svg class="src-chevron" :class="{ open: openDebug[index] }" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                  <polyline points="6,9 12,15 18,9"/>
                </svg>
              </button>
              <div v-show="openDebug[index]" class="debug-grid">
                <div class="debug-row">
                  <span class="debug-k">文档路由</span>
                  <span class="debug-v">
                    <template v-if="msg.retrieval.docRouter">
                      <span class="debug-num">保留 {{ msg.retrieval.docRouter.docs_kept }} / {{ msg.retrieval.docRouter.docs_total }} 份</span>
                      <span class="debug-why">{{ routerLabel(msg.retrieval.docRouter.reason) }}</span>
                      <span v-if="msg.retrieval.docRouter.fell_back" class="sources-warn"
                            title="按问题筛完之后候选为空，已回退为不筛选">已回退</span>
                      <span v-if="msg.retrieval.docRouter.min_docs" class="debug-why">阈值 {{ msg.retrieval.docRouter.min_docs }} 份</span>
                    </template>
                    <template v-else><span class="debug-why">（未记录）</span></template>
                  </span>
                </div>
                <div class="debug-row">
                  <span class="debug-k">召回</span>
                  <span class="debug-v"><span class="debug-num">{{ msg.retrieval.candidates }} 段</span></span>
                </div>
                <div class="debug-row">
                  <span class="debug-k">重排</span>
                  <span class="debug-v">
                    <span v-if="msg.retrieval.reranked">已重排</span>
                    <span v-else class="debug-why">未重排</span>
                    <span v-if="msg.retrieval.rerankBackend" class="debug-why">后端 {{ msg.retrieval.rerankBackend }}</span>
                    <span v-if="msg.retrieval.degradedReason" class="debug-why">{{ msg.retrieval.degradedReason }}</span>
                  </span>
                </div>
                <div class="debug-row">
                  <span class="debug-k">引用</span>
                  <span class="debug-v"><span class="debug-num">{{ (msg.sources || []).length }} 段进入答案</span></span>
                </div>
                <!-- ★ 联网搜索（§39.38）：让"这次联网到底有没有发生、走的哪个源、多久、为什么没结果"
                     全都可见。没开联网搜索时不显示这一行（attempted=false）。 -->
                <div v-if="msg.web && msg.web.attempted" class="debug-row">
                  <span class="debug-k">联网</span>
                  <span class="debug-v">
                    <template v-if="msg.web.count > 0">
                      <span class="debug-num">{{ msg.web.count }} 条</span>
                      <span class="debug-why">{{ msg.web.provider_label || msg.web.provider }}</span>
                      <span class="debug-why">{{ msg.web.elapsed_ms }} ms</span>
                      <span v-if="msg.web.cached" class="debug-why">缓存</span>
                    </template>
                    <template v-else>
                      <span class="debug-why">无结果</span>
                      <span class="debug-why">{{ msg.web.reason_text || msg.web.reason }}</span>
                    </template>
                    <span v-if="msg.web.providers_tried && msg.web.providers_tried.length > 1"
                          class="debug-why">试过 {{ msg.web.providers_tried.join(' → ') }}</span>
                  </span>
                </div>
              </div>
            </div>
            <div v-else-if="!msg.streaming && msg.role === 'assistant' && msg.sources && msg.sources.length" class="msg-debug-note">
              历史消息：仅保存了来源，未保存检索详情
            </div>

            <div v-if="!msg.streaming && msg.content" class="msg-actions">
              <button class="btn-action" title="复制" @click="copyMessage(msg.content)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                  <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>
              </button>
            </div>
          </div>
        </div>

        <!-- Loading -->
        <div v-if="isWaiting" class="message-row assistant">
          <div class="msg-avatar">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
              <path d="M12 2a4 4 0 0 1 4 4v1h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h2V6a4 4 0 0 1 4-4z"/>
            </svg>
          </div>
          <div class="msg-body">
            <div class="msg-bubble thinking">
              <span class="dot-pulse"></span>
            </div>
          </div>
        </div>

        <!-- ★ P1-1「回到底部」：只有【用户已经滚上去、且下面确实还有内容】时才出现。
             放在滚动容器**内部**、作为最后一个子元素，并用 `position: sticky` 钉在可视底部：
             这样它不会随内容滚走，也不会挤占布局（锚点 height:0）。
             `pointer-events` 锚点 none / 按钮 auto：那一条空带子不吃点击。 -->
        <div v-if="!following && canScroll" class="jump-bottom-anchor">
          <button class="btn-jump-bottom" @click="jumpToBottom()">
            {{ jumpLabel() }}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6,9 12,15 18,9"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Input -->
      <div class="input-area">
        <!-- 上传组件放在输入框【上方】：负责附件条 / 结果提示 / 我的知识库入口。
             它自己渲染在 .input-wrapper 之前，所以附件条正好出现在输入框上方。 -->
        <KnowledgeUpload ref="kbRef" />

        <div class="input-wrapper">
          <!-- 回形针：一般 AI 对话界面的附件按钮形态（点击 → 选 PDF） -->
          <button
            class="btn-attach"
            :disabled="isStreaming"
            :title="kbHasFile ? '已选择 PDF，点发送即可入库' : '上传 PDF 到我的知识库'"
            @click="kbRef?.pickFile()"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
          <textarea
            ref="inputEl"
            v-model="question"
            class="chat-input"
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            rows="1"
            :disabled="isStreaming"
            @keydown="onKeydown"
            @input="autoResize"
          ></textarea>
          <!-- ★ 「知识库搜索」：**常驻开关**，默认【开】= 默认走 RAG（用户 2026-10-12 拍板：
               默认一律先查知识库，别让判断在中间挡一道；开关关掉才交回 classify 逐题判断）。
               开 = 本条强制查库（**跳过 classify**，所以比自动判断还省一次调用）；
               关 = 交回逐题判断（问候类问题不再查库，也就不会再出现"判断为无需查库"的提示）。 -->
          <button
            class="btn-kbsearch"
            :class="{ on: forceSearch }"
            :disabled="isStreaming"
            :aria-pressed="forceSearch ? 'true' : 'false'"
            :title="forceSearch
              ? '已开启（默认）：提问一律先查知识库。点一下关掉，交回逐题判断'
              : '已关闭：交回逐题判断。点一下恢复「一律先查知识库」'"
            @click="toggleForceSearch"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>
            </svg>
            <span>知识库搜索</span>
          </button>
          <!-- ★ 「联网搜索」（§39.38）：与「知识库搜索」并列的**常驻开关**，默认【关】。
               开 = 本条去公开网页搜一次；结果作为【另一类资料】进回答，界面上单独一块列出，
               与「来源 N 段」（学校文件）措辞、配色都分开 —— 网页内容**绝不能**看起来像校规。
               关 = 一个字节都不多花（后端 prompt 与改造前逐字相同，也不外呼任何站点）。 -->
          <button
            class="btn-kbsearch is-web"
            :class="{ on: webSearch }"
            :disabled="isStreaming"
            :aria-pressed="webSearch ? 'true' : 'false'"
            :title="webSearch
              ? '已开启：本条会去公开网页搜一次。结果仅作参考，不是学校文件，不作为校规依据'
              : '已关闭：点一下让本条去公开网页搜一次（结果仅作参考，不是学校文件）'"
            @click="toggleWebSearch"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/>
              <path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>
            </svg>
            <span>联网搜索</span>
          </button>
          <button v-if="isStreaming" class="btn-stop" title="停止" @click="stopStreaming">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <rect x="4" y="4" width="16" height="16" rx="2"/>
            </svg>
          </button>
          <button v-else class="btn-send" :disabled="!question.trim() && !kbHasFile"
                  :title="kbHasFile ? '上传并解析入库' : '发送'" @click="send">
            <svg v-if="kbHasFile" width="17" height="17" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5,12 12,5 19,12"/>
            </svg>
            <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
              <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5,12 12,5 19,12"/>
            </svg>
          </button>
        </div>
        <p class="input-hint">AI 回答仅供参考，请以学校官方文件为准</p>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import 'highlight.js/styles/atom-one-light.css'
import { useAuth } from '../stores/auth.js'
import { apiGet, apiPost, apiPut, apiDelete, streamChat, createSseParser } from '../api/request.js'
import KnowledgeUpload from '../components/KnowledgeUpload.vue'

const router = useRouter()
const auth = useAuth()

// ========== Markdown ==========
// ★ marked v18 起 setOptions 的 highlight 选项已被【移除】，写在这里会被静默忽略 ——
//   实测渲染出的 <code> 没有任何 hljs-* 类，即语法高亮从未生效（而 highlight.js
//   仍被打进包里）。必须改用官方的 marked-highlight 扩展。
//   兜底刻意用 'plaintext' 而不是 hljs.highlightAuto()：后者每次渲染都要扫描全部语言，
//   而流式回答每收到一个分片就会整段重渲染一次。
marked.use(
  markedHighlight({
    emptyLangClass: 'hljs',
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      const language = hljs.getLanguage(lang) ? lang : 'plaintext'
      return hljs.highlight(code, { language }).value
    },
  }),
)

marked.setOptions({ breaks: true, gfm: true })

/**
 * 把回答正文里的引用编号 [n] 变成可点击角标（点击后展开对应来源）。
 *
 * ★ 必须先按 <pre>/<code> 把 HTML 切开，只处理【代码块之外】的片段：
 *   代码里到处是 a[0]、x[1] 这种下标，无脑全局替换会直接把用户看到的代码改坏。
 *   String.split 带捕获组时，分隔符（代码段）会落在奇数下标 —— 正好跳过它们。
 *
 * ★★ sources 参数（第十二轮加）：**只有真的存在对应来源的编号才配当"依据"**。
 *   提示词里写了"绝不编造编号"（rag_template 规则 14），但那只是**叮嘱**：
 *   一旦模型写上 [7] 而本条只有 5 条来源，旧实现照样把它渲染成和真引用**一模一样**、
 *   还带 tooltip「查看来源 7」的可点角标 —— 点下去毫无反应。
 *   那等于 UI 承诺了它没有的依据（与 §39.25 要消灭的假信号同一族）。
 *   现在这种编号渲染成「幽灵角标」：**字符照留**（绝不改动回答原文一个字），
 *   但样式与文案都表明"没有对应来源"，不留任何"可点即有效"的暗示。
 */
function linkifyCitations(html, sources) {
  if (!html || html.indexOf('[') === -1) return html
  const available = new Set(
    (Array.isArray(sources) ? sources : [])
      .map(s => Number(s && s.n))
      .filter(n => Number.isInteger(n) && n > 0)
  )
  return html
    .split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
    .map((seg, i) => {
      if (i % 2 === 1) return seg          // 奇数位 = 代码段，原样放回
      return seg.replace(/\[(\d{1,2})\]/g, (whole, num) => (
        available.has(Number(num))
          ? `<sup class="cite-ref" data-cite="${num}" title="查看来源 ${num}">${num}</sup>`
          : `<sup class="cite-ref is-ghost" data-cite="${num}" title="本条没有来源 ${num} —— 编号可能是模型编造的，请勿当作依据">${num}</sup>`
      ))
    })
    .join('')
}

function renderMarkdown(text, sources) {
  if (!text) return ''
  try {
    return linkifyCitations(marked.parse(text), sources)
  } catch {
    return text.replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }
}

// ========== State ==========
const question = ref('')
const messages = ref([])
const conversations = ref([])
const activeConvId = ref(null)
const isStreaming = ref(false)
const isWaiting = ref(false)
const sidebarCollapsed = ref(false)
const messagesContainer = ref(null)
// —— P0-4：侧栏搜索 / 重命名 / 删除二次确认 ——
const convQuery = ref('')            // 搜索关键词（只匹配标题）
const renamingId = ref(null)         // 正在重命名的对话 id
const renameText = ref('')           // 重命名输入框内容
const confirmDeleteId = ref(null)    // 正在等待"确认删除"的对话 id

// 搜索只过滤标题：后端 /conversations 不接搜索参数，一次就把全部会话返回了。
// 空关键词原样返回，避免列表看起来"少了几条"。
const filteredConversations = computed(() => {
  const q = convQuery.value.trim().toLowerCase()
  if (!q) return conversations.value
  return conversations.value.filter(c => (c.title || '').toLowerCase().includes(q))
})
// 来源面板展开状态：{ [消息下标]: true }
const openSources = ref({})
// 检索详情面板的展开状态。与 openSources 分开：两者独立展开、互不干扰。
const openDebug = ref({})
// 当前高亮的角标，形如 "3:2"（第 3 条消息的来源 2）
const activeCite = ref('')
const inputEl = ref(null)
let abortController = null

// 上传组件（持有附件条与隐藏的 file input）。回形针按钮调它的 pickFile()，
// 发送按钮在有附件时调它的 startUpload() —— 后者是 standard AI 对话界面的行为。
const kbRef = ref(null)
const kbHasFile = computed(() => !!kbRef.value?.hasFile)

const welcomeHints = [
  '国家奖学金申请条件是什么？',
  '综测加分项目有哪些？',
  '奖学金的评定流程是怎样的？',
  '如何提高综测成绩？',
]

// ========== Lifecycle ==========
onMounted(() => {
  loadConversations()
})

async function loadConversations() {
  try {
    conversations.value = await apiGet('/conversations')
    // Auto-select most recent
    if (conversations.value.length > 0 && !activeConvId.value) {
      await switchChat(conversations.value[0].id, /* silent */ true)
    }
  } catch (e) {
    console.error('加载对话列表失败:', e)
  }
}

// ========== Chat Management ==========
async function newChat() {
  if (isStreaming.value) stopStreaming()
  // 新对话必须清掉搜索词：否则新对话不匹配当前过滤条件，用户会以为没创建成功
  convQuery.value = ''
  cancelDelete()
  cancelRename()
  try {
    const conv = await apiPost('/conversations', { title: '新对话' })
    conversations.value.unshift(conv)
    activeConvId.value = conv.id
    messages.value = []
    // 新对话没有消息了，把来源面板的展开/高亮状态一并清掉 ——
    // 否则上一条对话里下标相同的来源面板会"继承"展开态。
    openSources.value = {}
    activeCite.value = ''
    sidebarCollapsed.value = false
    nextTick(() => inputEl.value?.focus())
  } catch (e) {
    console.error('创建对话失败:', e)
  }
}

function toggleSources(i) {
  openSources.value[i] = !openSources.value[i]
}

function toggleDebug(i) {
  openDebug.value[i] = !openDebug.value[i]
}

/** 文档路由 reason → 人话。
 *  ★ 取值必须覆盖 langchain_project.route_doc_ids() 写进 trace 的【全部】reason；
 *    认不出的原因就原样显示（绝不留空、绝不谎报成"已启用"）。
 *    有一个脚本 _debug_panel_test.mjs 专门做前后端字段对齐检查。 */
const ROUTER_LABELS = {
  filtered: '已按问题筛掉无关文档',
  too_few_docs: '文档太少，不筛选',
  no_match_or_would_be_empty: '没匹配上任何文档，不筛选',
  disabled_by_env: '路由器被环境变量关闭',
  no_index: '还没有可用的文档索引',
  index_error: '索引构建出错，已降级为不筛选',
}
function routerLabel(reason) {
  if (!reason) return '（未记录）'
  return ROUTER_LABELS[reason] || ('未知原因：' + reason)
}

/** 点击正文角标 [n]：展开该条消息的来源面板并高亮对应条目 */
function onCiteClick(e, msgIndex) {
  const el = e.target.closest('.cite-ref')
  if (!el) return
  const n = Number(el.dataset.cite)
  const msg = messages.value[msgIndex]
  // ★ 模型可能编造编号：只响应真实存在的来源，绝不给"幻觉引用"任何反馈。
  //   （渲染侧已经把这种编号标成 .is-ghost 了 —— 见 linkifyCitations；
  //     这里再挡一次是有意的【双保险】：样式可能被主题改掉，而"点了没反应"不该有歧义。）
  if (!msg || !Array.isArray(msg.sources) || !msg.sources.some(s => s.n === n)) return
  openSources.value[msgIndex] = true
  activeCite.value = msgIndex + ':' + n
}

async function switchChat(id, silent = false) {
  if (id === activeConvId.value && !silent) return
  if (isStreaming.value) stopStreaming()
  // 换会话时收起编辑态/确认态 —— 它们属于原来那一行
  cancelDelete()
  cancelRename()
  try {
    const detail = await apiGet(`/conversations/${id}`)
    activeConvId.value = id
    messages.value = detail.messages.map(m => ({
      role: m.role,
      content: m.content,
      streaming: false,
      // ★ 历史来源由后端一起带回（message_sources 表）。
      //   不带就等于"刷新即失忆"：正文里 [1][2] 还在，却查不到任何依据。
      sources: m.sources || [],
      retrieval: null,
      // ★ 网页来源**不落库**（message_sources 只存学校文档引用）⇒ 历史消息上看不到网页列表。
      //   这里显式写成 null，而不是靠"字段不存在"——两者的渲染结果一样，
      //   但显式写出来能让下一个人一眼看出"这是有意不持久化"，不是漏了接线。
      web: null,
    }))
    // 换会话要清掉展开/高亮状态 —— 下标已经不再对应同一条消息了
    openSources.value = {}
    activeCite.value = ''
    nextTick(() => jumpToBottom())   // ★ P1-1：切换会话 = 离散动作 ⇒ 到底并恢复跟随
  } catch (e) {
    console.error('加载对话失败:', e)
  }
}

// ========== P0-4：搜索 / 重命名 / 删除二次确认 ==========
function askDelete(id) { confirmDeleteId.value = id }
function cancelDelete() { confirmDeleteId.value = null }

/** 行体点击：确认态下点行体 = 放弃删除；重命名中不切走 */
function onConvClick(id) {
  if (confirmDeleteId.value === id) { cancelDelete(); return }
  if (renamingId.value !== null) return
  switchChat(id)
}

async function startRename(conv) {
  confirmDeleteId.value = null
  renamingId.value = conv.id
  renameText.value = conv.title || ''
  await nextTick()
  // 同一时刻只可能有一个重命名输入框，直接取就行
  const el = document.querySelector('.chat-item-rename')
  el?.focus()
  el?.select()
}

function cancelRename() {
  renamingId.value = null
  renameText.value = ''
}

/** Enter / 失焦都会走到这里 —— 靠 renamingId 判重：
 *  Esc 取消后紧随的 blur 会发现 renamingId 已不是本行，直接早退，
 *  不会把"取消"变成"保存"。 */
async function commitRename(id) {
  if (renamingId.value !== id) return
  const title = renameText.value.trim()
  renamingId.value = null
  renameText.value = ''
  if (!title) return                              // 空标题视为取消，不改动
  const conv = conversations.value.find(c => c.id === id)
  if (!conv || conv.title === title) return        // 没变就不发请求
  const old = conv.title
  conv.title = title                               // 乐观更新：输入完立刻看到新名字
  try {
    const saved = await apiPut(`/conversations/${id}`, { title })
    if (saved && saved.title) conv.title = saved.title
  } catch (e) {
    conv.title = old                               // 失败回滚 —— 界面不能说谎
    console.error('重命名失败:', e)
  }
}

async function deleteChat(id) {
  confirmDeleteId.value = null
  if (renamingId.value === id) cancelRename()
  try {
    await apiDelete(`/conversations/${id}`)
    conversations.value = conversations.value.filter(c => c.id !== id)
    if (activeConvId.value === id) {
      activeConvId.value = null
      messages.value = []
      if (conversations.value.length > 0) {
        await switchChat(conversations.value[0].id, true)
      }
    }
  } catch (e) {
    console.error('删除对话失败:', e)
  }
}

// ========== Logout ==========
function handleLogout() {
  auth.logout()
  router.push('/login')
}

// ========== Input ==========
function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

// ========== Auto-scroll（P1-1：**黏底**策略 · v2 = 按【用户意图】而不是【位置】）==========
// ★ 史前行为：每读完一个网络分片就 `await scrollToBottom()` **无条件**拉到底 ——
//   一次回答几十次，用户往回翻时手还没停稳就被拽下去；`smooth` 动画被逐分片重启，观感一顿一顿。
// ★ v1（**已作废**）：判据是【距底 ≤ NEAR_BOTTOM_PX 才跟随】。它有一个致命缺陷 ——
//   **内容自己在长的时候距底会自然变大**，于是用户明明没动滚轮，跟随也会自己停掉
//   （用户 2026-10-13 实测报的正是这个："不需要向上翻阅的时候也无法在输出的时候自动往下"）。
//   病根见下面 `progTarget` 那段注释：**我们自己触发的 scroll 事件被当成了"用户滚走了"**。
// ★ v2 的规则（同样只有三条）：把"停"的判据从**位置**换成**用户的意图** ——
//   ① 只有【用户自己往上滚】（滚轮上滚 / 触摸下拉 / 键盘上翻 / 拖动滚动条向上）才停止跟随；
//   ② 用户没动滚轮 ⇒ 逐分片**一律继续跟到底**（此刻离底多远都无所谓，那是内容在长）；
//   ③ 用户滚回底部附近 / 点「回到底部」/ 自己按发送 ⇒ 恢复跟随并到底。
// ★ 【程序滚动 ≠ 用户意图】：我们自己的 `scrollTo` 同样会触发 scroll 事件。v1 把它当成用户意图，
//   而"这一帧内容又长了一截"时那条事件看起来就像"用户滚远了" ⇒ 跟随被永久停掉。
//   现在记下**我们自己设定的目标位置** `progTarget`，与之相差 ≤1px 的 scroll 事件直接忽略。
// ★ 逐分片用 `instant` 而不是 `smooth`：smooth 每次都要重新起一段动画，几十次叠加就是抖动。
const NEAR_BOTTOM_PX = 60          // 距底多少像素内算"在底部附近"（钉死语义：60 算，61 不算）
const TOUCH_INTENT_PX = 8          // 触摸：手指下移超过这么多像素才算"用户想看上文"
const following = ref(true)        // 是否跟随底部
const canScroll = ref(false)       // 内容是否真的超出可视高度（没超出就不该出现按钮）
const hasNewBelow = ref(false)     // 停止跟随后是否又来了新内容（按钮文案用）
let progTarget = null              // 上一次**程序**滚动设定的目标（用来把"自己滚的"与"用户滚的"分开）
let lastTouchY = 0                 // 触摸起点（判断"往下拖"）

/** 距底部是否在阈值内。★ 单独抽出来，是为了让测试能直接钉住这个语义。 */
function isNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX
}

/** 内容是否超出可视高度（按钮只在"确实还有内容在下面"时才有意义）。 */
function syncScrollState() {
  const el = messagesContainer.value
  if (!el) return
  canScroll.value = el.scrollHeight > el.clientHeight + 1
}

/** 停止跟随（用户想看上文）。★ 只有"用户意图"该调它。 */
function stopFollowing() {
  following.value = false
}

/** 恢复跟随（用户回到下面来了）。 */
function resumeFollowing() {
  following.value = true
  hasNewBelow.value = false
}

/** 滚轮：**上滚立刻停**（哪怕只滚 5px —— 位置判据在这里正好是瞎的）；下滚回到底部附近就恢复。 */
function onWheel(e) {
  if (!e || typeof e.deltaY !== 'number') return
  if (e.deltaY < 0) { stopFollowing(); return }
  if (e.deltaY > 0) {
    const el = messagesContainer.value
    if (el && isNearBottom(el)) resumeFollowing()
  }
}

/** 触摸：记住手指起点。 */
function onTouchStart(e) {
  const t = e && e.touches && e.touches[0]
  if (t) lastTouchY = t.clientY
}

/** 触摸：手指往下拖 = 想看上文 ⇒ 停。 */
function onTouchMove(e) {
  const t = e && e.touches && e.touches[0]
  if (!t) return
  if (t.clientY - lastTouchY > TOUCH_INTENT_PX) stopFollowing()
  lastTouchY = t.clientY
}

/** 键盘上翻（容器内有焦点时才会收到；位置判据仍会兜住键盘滚动）。
 *  ★ 名字**不能**叫 `onKeydown` —— 本文件输入框那个 `onKeydown` 已存在，
 *    重名会让 SFC 编译直接报 "Identifier 'onKeydown' has already been declared"（_debug_panel_test.mjs 抓到的）。 */
function onMessagesKeydown(e) {
  const k = e && e.key
  if (k === 'ArrowUp' || k === 'PageUp' || k === 'Home') stopFollowing()
}

/** 滚动容器的 scroll 事件：负责"识别**用户**拖动"与"滚回底部就恢复"，不再拿位置当唯一判据。 */
function onMessagesScroll() {
  const el = messagesContainer.value
  if (!el) return
  syncScrollState()
  // ★ 这一条是 v2 的核心：**我们自己滚的那次**直接忽略（否则 v1 的病根会原样回来）
  if (progTarget !== null && Math.abs(el.scrollTop - progTarget) <= 1) {
    progTarget = null
    return
  }
  progTarget = null
  if (isNearBottom(el)) resumeFollowing()   // 用户滚回下面了
  else stopFollowing()                      // 用户拖滚动条往上（这类滚动**没有** wheel 事件）
}

async function scrollToBottom(smooth = true) {
  await nextTick()
  const el = messagesContainer.value
  if (!el) return
  // 先记下"我们要滚到哪里"（夹到合法范围后），供 onMessagesScroll 分辨"自己滚的"
  progTarget = Math.min(el.scrollHeight, Math.max(0, el.scrollHeight - el.clientHeight))
  el.scrollTo({
    top: el.scrollHeight,
    behavior: smooth ? 'smooth' : 'instant',
  })
}

/** 流式期间逐分片调用：**只有"用户想看上文"时才什么都不做**（不再看位置）。 */
async function followBottom() {
  await nextTick()
  syncScrollState()
  if (!following.value) {
    hasNewBelow.value = true
    return
  }
  await scrollToBottom(false)      // ★ 逐分片必须是 instant
}

/** 用户明确要到底：发送消息 / 点「回到底部」。 */
async function jumpToBottom() {
  resumeFollowing()
  await scrollToBottom(false)
  await nextTick()
  syncScrollState()
}

/** 按钮文案：停止跟随之后是「有新内容」还是普通「回到底部」。 */
function jumpLabel() {
  return hasNewBelow.value ? '有新内容' : '回到底部'
}

// ========== Send ==========
// ★ 「知识库搜索」：**常驻开关 + 跨刷新记住**，默认【开】（= 默认走 RAG，用户 2026-10-12 拍板）。
//   · 开：本条强制查库，后端跳过 classify（少一次调用）
//   · 关：交回 classify 逐题判断
//   ★ 刻意**不在 send() 里复位**：它是常驻设置，不是"只影响本条"的一次性开关。
//   ★ 持久化（用户同日追加要求"跨刷新记住"）—— 上一版我主张只在内存里常驻、不写存储，
//     理由是"某天关过一次会变成看不见的持久状态"；用户明确要跨刷新记住 ⇒ 按用户的要求做。
//     缓解措施：只认 '1'/'0'，脏值/读失败/存失败一律**退回默认并照常工作**（绝不让存储问题挡住提问）。
const FORCE_SEARCH_KEY = 'kb_force_search'   // 键名与 auth_token 同风格：扁平、无双前缀
const FORCE_SEARCH_DEFAULT = true            // ★ 默认【开】= 默认走 RAG（需求，不是实现细节）
function loadForceSearch() {
  try {
    const v = localStorage.getItem(FORCE_SEARCH_KEY)
    // 只认这两个值：半损坏/被手改过的值一律当"没存过"，避免把脏数据当成用户意图
    return v === '1' ? true : v === '0' ? false : FORCE_SEARCH_DEFAULT
  } catch (e) {
    return FORCE_SEARCH_DEFAULT              // 隐私模式 / 被禁用 / 非浏览器环境 ⇒ 不崩
  }
}
const forceSearch = ref(loadForceSearch())
function toggleForceSearch() {
  forceSearch.value = !forceSearch.value
  try {
    localStorage.setItem(FORCE_SEARCH_KEY, forceSearch.value ? '1' : '0')
  } catch (e) {
    // 存不了就算了：**本次会话仍然生效**（不影响本条提问走哪条路）
  }
}

// ★ 「联网搜索」（§39.38）：与上面的「知识库搜索」**同一套写法**（同风格、同样跨刷新记住）。
//   为什么默认【关】而不是像知识库搜索那样默认开：
//     ① 联网要**外呼第三方站点**、有延迟（实测 +1.2 秒左右），默认开等于给每条提问加成本；
//     ② 网络内容**不是学校文件**，默认把它塞进回答会让"依据"变浑 —— 校规问题应当以库为准；
//     ③ 用户要联网时点一下即可（状态跨刷新记住），比"默认开、每次去关"更省事。
//   同样的缓解措施：只认 '1'/'0'，脏值/读失败/存失败一律退回默认并照常工作。
const WEB_SEARCH_KEY = 'kb_web_search'
const WEB_SEARCH_DEFAULT = false
function loadWebSearch() {
  try {
    const v = localStorage.getItem(WEB_SEARCH_KEY)
    return v === '1' ? true : v === '0' ? false : WEB_SEARCH_DEFAULT
  } catch (e) {
    return WEB_SEARCH_DEFAULT                 // 隐私模式 / 被禁用 / 非浏览器环境 ⇒ 不崩
  }
}
const webSearch = ref(loadWebSearch())
function toggleWebSearch() {
  webSearch.value = !webSearch.value
  try {
    localStorage.setItem(WEB_SEARCH_KEY, webSearch.value ? '1' : '0')
  } catch (e) {
    // 存不了也照常工作（同上）
  }
}

async function send() {
  // ★ 已选中 PDF 时，发送 = 上传并解析入库（一般 AI 对话界面的行为：
  //   附件先挂上，再点发送）。kbHasFile 在纯聊天时恒为 false，
  //   所以下面原有的聊天分支完全不受影响。
  if (kbHasFile.value) {
    if (isStreaming.value) return
    await kbRef.value.startUpload()
    return
  }

  const q = question.value.trim()
  if (!q || isStreaming.value) return

  // ★ 常驻开关：直接读当前状态，**不复位**（默认开 ⇒ 每条都强制查库）。
  //   空消息早退时也不会改动它（上面已 return）。
  const forceRag = forceSearch.value
  // ★ 联网搜索同理：读当前状态、不复位（默认关 ⇒ 只有用户点开才外呼）。
  const useWeb = webSearch.value

  question.value = ''
  if (inputEl.value) inputEl.value.style.height = 'auto'

  // Auto-create conversation if needed
  if (!activeConvId.value) {
    try {
      const conv = await apiPost('/conversations', {
        title: q.length > 30 ? q.slice(0, 30) + '...' : q,
      })
      activeConvId.value = conv.id
      conversations.value.unshift(conv)
    } catch (e) {
      console.error('创建对话失败:', e)
      return
    }
  }

  // Update sidebar title for first message
  const conv = conversations.value.find(c => c.id === activeConvId.value)
  if (conv && messages.value.length === 0) {
    conv.title = q.length > 30 ? q.slice(0, 30) + '...' : q
  }

  messages.value.push({ role: 'user', content: q, streaming: false })
  messages.value.push({ role: 'assistant', content: '', streaming: true,
                        sources: [], retrieval: null, web: null })

  await jumpToBottom()   // ★ P1-1：自己按的发送 ⇒ 强制到底（不看"跟不跟随"）

  const aiIndex = messages.value.length - 1
  isStreaming.value = true
  isWaiting.value = true

  abortController = new AbortController()

  try {
    const response = await streamChat(q, activeConvId.value, abortController.signal, forceRag, useWeb)

    if (!response.ok) throw new Error(`HTTP ${response.status}`)

    const reader = response.body.getReader()
    const decoder = new TextDecoder()

    // ★ 用真正的 SSE 解析器，取代原来那条"把 data: 和换行全删掉"的正则 ——
    //   那一句会把段落的换行一并删掉，多段回答在页面上会被压成一行。
    // ★ 按 event 分派：sources 帧是独立载荷（JSON），绝不能混进正文。
    const parser = createSseParser(({ event, data }) => {
      if (event === 'sources') {
        try {
          const payload = JSON.parse(data)
          messages.value[aiIndex].sources = payload.sources || []
          messages.value[aiIndex].retrieval = {
            attempted: !!payload.attempted,
            candidates: payload.candidates || 0,
            // ★ 把"重排降级"透到 UI：后端重排服务偶发返回空结果时会回退成
            //   原始召回顺序，此时 reranked === false。不显示出来，
            //   用户就无从知道这次的来源顺序其实没经过重排。
            reranked: payload.reranked !== false,
            degradedReason: payload.degraded_reason || '',
            // ★ 检索详情面板要用的流水线事实。
            //   这两个字段【后端本来就随同一个 sources 帧送来了】，只是以前在这里被丢掉。
            //   补上它们不需要改任何后端代码（trace 就是整个 payload）。
            rerankBackend: payload.rerank_backend || '',
            docRouter: payload.doc_router || null,
          }
          // ★ 联网搜索（§39.38）：**两条路径都会带**这个键（走 RAG 时是 trace 本身，
          //   走直答时是状态帧）。没开联网搜索时它可能是 null/缺失 ⇒ 一律当"没搜过"。
          //   ⚠️ 网页来源**不落库**（message_sources 表只存学校文档引用）⇒ 切换会话/刷新后
          //   这个列表不再显示；正文里由模型写明的"来自网络"仍然在。这是**已知边界**，不是 bug。
          messages.value[aiIndex].web = payload.web || null
        } catch (e) {
          // 载荷坏了只该影响来源面板，不能连累正文
          console.warn('来源帧解析失败:', e)
        }
        return
      }
      messages.value[aiIndex].content += data
    })

    isWaiting.value = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      parser.push(decoder.decode(value, { stream: true }))
      await followBottom()   // ★ P1-1：逐分片只"跟随"，用户滚上去了就绝不动他的滚动条
    }
    parser.flush()
  } catch (err) {
    if (err.name !== 'AbortError') {
      messages.value[aiIndex].content += `\n\n> ⚠️ 请求失败：${err.message}`
    }
  } finally {
    messages.value[aiIndex].streaming = false
    isStreaming.value = false
    isWaiting.value = false
    abortController = null
  }
}

function sendHint(hint) {
  question.value = hint
  send()
}

function stopStreaming() {
  if (abortController) {
    abortController.abort()
    abortController = null
  }
}

async function copyMessage(text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

// ========== Auto-scroll ==========
// ★ P1-1：这里 watch 的是**消息条数**（离散事件：发消息 / 载入会话），所以可以强制到底；
//   流式正文的逐分片增长**不走这里**（它走 followBottom，用户滚上去就不动）。
watch(() => messages.value.length, () => jumpToBottom())
</script>

<style scoped>
/* ========== CSS Variables (Light Theme) ========== */
.app-shell {
  --bg-primary: #f5f6f8;
  --bg-secondary: #ffffff;
  --bg-surface: #ffffff;
  --bg-hover: #f0f1f5;
  --bg-input: #f0f2f5;
  --text-primary: #1a1a2e;
  --text-secondary: #5a5a72;
  --text-muted: #999aae;
  --accent: #4d6bfe;
  --accent-light: #5b78ff;
  --accent-dim: rgba(77, 107, 254, 0.08);
  --user-bubble: #e8edff;
  --user-bubble-hover: #dbe3ff;
  --assistant-bubble: #ffffff;
  --assistant-border: #e0e2ea;
  --border: #e2e4ec;
  --border-light: #eef0f5;
  --success: #10b981;
  --error: #ef4444;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.06);
  --duration-fast: 150ms;
  --duration-normal: 250ms;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);
  --radius-sm: 8px;
  --radius-md: 14px;
  --radius-lg: 20px;
  --radius-full: 9999px;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;

  display: flex;
  height: 100vh;
  background: var(--bg-primary);
  font-family: var(--font-sans);
  color: var(--text-primary);
}

/* ========== Sidebar ========== */
.sidebar {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  transition: width var(--duration-normal) var(--ease-out);
  overflow: hidden;
}
.sidebar.collapsed { width: 52px; }

.sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 12px;
  border-bottom: 1px solid var(--border);
}

.sidebar-title {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.chat-item {
  display: flex;
  align-items: center;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-secondary);
  transition: background var(--duration-fast);
}
.chat-item:hover { background: var(--bg-hover); }
.chat-item.active {
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 500;
}

.chat-item-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btn-delete {
  opacity: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 4px;
  transition: opacity var(--duration-fast);
}
.chat-item:hover .btn-delete { opacity: 1; }
.btn-delete:hover { color: var(--error); }

.sidebar-empty {
  padding: 16px;
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
}

/* ---- P0-4：侧栏搜索 ---- */
.conv-search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 4px 6px;
  padding: 0 8px;
  height: 30px;
  background: var(--bg-input);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  transition: border-color var(--duration-fast), background var(--duration-fast);
}
.conv-search:focus-within {
  border-color: var(--accent);
  background: var(--bg-secondary);
}
.conv-search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  outline: none;
  font-family: var(--font-sans);
  font-size: 12.5px;
  color: var(--text-primary);
}
.conv-search-input::placeholder { color: var(--text-muted); }
.conv-search-clear {
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 15px;
  line-height: 1;
  padding: 0 2px;
  border-radius: 4px;
}
.conv-search-clear:hover { color: var(--text-primary); }

/* ---- P0-4：行内重命名 ---- */
.chat-item-rename {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--accent);
  border-radius: 5px;
  background: var(--bg-secondary);
  outline: none;
  padding: 2px 6px;
  font-family: var(--font-sans);
  font-size: 13px;
  color: var(--text-primary);
}

/* ---- P0-4：删除二次确认 ----
   刻意不用实心红按钮：那会成为全站最响的元素，与整体克制的配色冲突。
   用淡红底 + 红字，既和"取消"一眼可分，又不喧宾夺主。 */
.btn-confirm {
  flex-shrink: 0;
  border: 1px solid transparent;
  background: rgba(239, 68, 68, 0.10);
  color: var(--error);
  font-family: var(--font-sans);
  font-size: 12px;
  line-height: 1;
  padding: 4px 8px;
  border-radius: 5px;
  cursor: pointer;
  margin-left: 4px;
  white-space: nowrap;
  transition: background var(--duration-fast);
}
.btn-confirm:hover { background: rgba(239, 68, 68, 0.18); }
.btn-confirm.ghost {
  background: transparent;
  color: var(--text-secondary);
  border-color: var(--border);
}
.btn-confirm.ghost:hover { background: var(--bg-hover); color: var(--text-primary); }

/* ========== Main ========== */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  background: var(--bg-secondary);
}

.header-left, .header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-title {
  font-size: 15px;
  font-weight: 600;
}

.user-tag {
  font-size: 13px;
  color: var(--text-secondary);
  padding: 4px 10px;
  background: var(--bg-input);
  border-radius: var(--radius-full);
}

/* ========== Buttons ========== */
.btn-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--duration-fast);
}
.btn-icon:hover { background: var(--bg-hover); }

.btn-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-family: inherit;
  transition: all var(--duration-fast);
}
.btn-text:hover { background: var(--bg-hover); }

.btn-action {
  display: inline-flex;
  padding: 3px 6px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 4px;
}
.btn-action:hover { color: var(--text-secondary); }

/* ========== Messages ========== */
.messages-area {
  flex: 1;
  overflow-y: auto;
  padding: 20px 0;
}

/* ★ P1-1「回到底部」：sticky 锚点在滚动容器内钉住可视底部；
   height:0 ⇒ 不占布局；锚点 pointer-events:none + 按钮 auto ⇒ 那条空带子不吃点击。 */
.jump-bottom-anchor {
  position: sticky;
  bottom: 14px;
  height: 0;
  display: flex;
  justify-content: center;
  pointer-events: none;
}
.btn-jump-bottom {
  pointer-events: auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.4;
  cursor: pointer;
  box-shadow: var(--shadow-md);
}
.btn-jump-bottom:hover {
  color: var(--text-primary);
  border-color: var(--accent);
}

/* Welcome */
.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 20px;
  text-align: center;
}

.welcome-icon {
  width: 80px;
  height: 80px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-dim);
  border-radius: var(--radius-lg);
  color: var(--accent);
  margin-bottom: 24px;
}

.welcome-heading { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
.welcome-sub { font-size: 14px; color: var(--text-muted); margin-bottom: 32px; }

.welcome-hints {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  max-width: 500px;
}

.hint-chip {
  padding: 8px 18px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  border-radius: var(--radius-full);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: all var(--duration-fast);
}
.hint-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-dim);
}

/* ========== Message Row ========== */
.message-row {
  display: flex;
  gap: 12px;
  padding: 12px 20px;
  max-width: 820px;
  margin: 0 auto;
  width: 100%;
  animation: fadeIn 0.3s var(--ease-out);
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.message-row.user { flex-direction: row-reverse; }

.msg-avatar {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  margin-top: 2px;
}
.message-row.user .msg-avatar {
  background: var(--accent-dim);
  color: var(--accent);
}
.message-row.assistant .msg-avatar {
  background: var(--bg-hover);
  color: var(--text-secondary);
}

.msg-body { min-width: 0; display: flex; flex-direction: column; }
.message-row.user .msg-body { align-items: flex-end; }
.message-row.assistant .msg-body { align-items: flex-start; }

/* ========== Bubble ========== */
.msg-bubble {
  padding: 12px 18px;
  border-radius: var(--radius-md);
  font-size: 14.5px;
  line-height: 1.7;
  word-wrap: break-word;
  overflow-wrap: break-word;
}

.message-row.user .msg-bubble {
  background: var(--user-bubble);
  border-bottom-right-radius: 4px;
  color: #1a1a2e;
}

.message-row.assistant .msg-bubble {
  background: var(--assistant-bubble);
  border: 1px solid var(--assistant-border);
  border-bottom-left-radius: 4px;
}

.msg-bubble.thinking { padding: 16px 20px; }

/* ========== Typing ========== */
.typing-cursor {
  display: inline-block;
  color: var(--accent);
  animation: blink 1s step-end infinite;
  font-weight: 100;
  margin-left: 2px;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

.dot-pulse {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  animation: pulse 1.4s ease-in-out infinite;
  position: relative;
}
.dot-pulse::before, .dot-pulse::after {
  content: '';
  position: absolute;
  top: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}
.dot-pulse::before { left: -16px; animation: pulse 1.4s ease-in-out 0.2s infinite; }
.dot-pulse::after  { left: 16px;  animation: pulse 1.4s ease-in-out 0.4s infinite; }

@keyframes pulse {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

.msg-actions { margin-top: 4px; padding: 0 4px; }

/* ========== Input ========== */
.input-area {
  flex-shrink: 0;
  padding: 12px 20px 16px;
  background: var(--bg-secondary);
  border-top: 1px solid var(--border);
}

.input-wrapper {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  max-width: 820px;
  margin: 0 auto;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 6px 8px 6px 16px;
  transition: border-color var(--duration-fast);
}
.input-wrapper:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(77, 107, 254, 0.1);
}

.chat-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font-family: inherit;
  font-size: 14.5px;
  line-height: 1.6;
  resize: none;
  max-height: 160px;
  padding: 4px 0;
}
.chat-input::placeholder { color: var(--text-muted); }
.chat-input:disabled { opacity: 0.6; }

.btn-send, .btn-stop {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--duration-fast);
}

.btn-send {
  background: var(--accent);
  color: #fff;
}
.btn-send:hover:not(:disabled) { background: #3d5bef; }
.btn-send:disabled { opacity: 0.35; cursor: not-allowed; }

.btn-stop {
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.btn-stop:hover { color: var(--text-primary); }

/* 「知识库搜索」：输入框右侧的一次性开关。
   开启态用【强调色】而不是警示色 —— 全站唯一的警示色留给"未重排"（§39.11-4）。 */
.btn-kbsearch {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 34px;
  padding: 0 10px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: inherit;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
  transition: all var(--duration-fast);
}
.btn-kbsearch:hover:not(:disabled) { background: var(--bg-input); color: var(--text-primary); }
.btn-kbsearch:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-kbsearch.on {
  background: rgba(77, 107, 254, 0.12);
  border-color: var(--accent);
  color: var(--accent);
}
/* ★ 「联网搜索」（§39.38）：开启态刻意不用强调色，而用**偏暖的中性色** ——
   它与「知识库搜索」不是同一类动作：一个查**学校文件**（可信依据），
   一个查**公开网页**（只作参考）。两者同色会让人以为网页也是"依据"。 */
.btn-kbsearch.is-web.on {
  background: rgba(240, 173, 78, 0.14);
  border-color: #e0a355;
  color: #c98a2b;
}

/* ---- 网页参考块（与「来源 N 段」视觉、措辞都分开） ---- */
.msg-web {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px dashed #d9b57a;
  border-left: 3px solid #e0a355;
  border-radius: var(--radius-sm);
  background: rgba(240, 173, 78, 0.06);
}
.web-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.web-badge { font-size: 12px; font-weight: 600; color: #b8791f; }
.web-note { font-size: 11px; color: var(--text-secondary); }
.web-list { margin: 6px 0 0; padding-left: 18px; }
.web-list li { margin-bottom: 6px; font-size: 12px; line-height: 1.5; }
.web-title { color: #b8791f; text-decoration: none; word-break: break-all; }
.web-title:hover { text-decoration: underline; }
.web-site {
  margin-left: 6px;
  font-size: 11px;
  color: var(--text-secondary);
}
.web-snippet { color: var(--text-secondary); }
.web-empty { font-size: 12px; color: var(--text-secondary); }

.input-hint {
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 8px;
}

/* ========== Responsive ========== */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0; top: 0; bottom: 0;
    z-index: 100;
    box-shadow: 0 8px 32px rgba(0,0,0,0.12);
  }
  .sidebar.collapsed { width: 0; border-right: none; }
  .sidebar.collapsed .sidebar-header { display: none; }

  .message-row { padding: 10px 12px; }
  .msg-bubble { padding: 10px 14px; font-size: 14px; }
  .input-area { padding: 8px 12px 12px; }
  .welcome-heading { font-size: 20px; }
}
</style>
