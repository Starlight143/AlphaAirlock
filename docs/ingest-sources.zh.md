# 資料源訂閱 — URL / 識別碼 格式指南

> **繁體中文** · [English](./ingest-sources.md)

本頁說明 **`/sources`（來源管理）** 頁面上每一種來源型別，在 **URL / Identifier**
欄位該填什麼，以及後端如何把你填的值轉成可輪詢的 feed。

最重要的一條規則：

> **通用的 `rss` 型別「不會」從首頁自動探測 feed。**
> 你填什麼就原封不動丟給 feed parser。`rss` 必須填**feed 網址本身**
> （`…/feed`、`…/rss`、`…/feed.xml`）。而平台專屬型別
> （Substack / Medium / Reddit / YouTube / Twitter）**可以**直接貼作者的
> 首頁／個人頁網址——fetcher 會幫你推導出 feed 端點。

以下內容的權威來源：
[`backend/core/database.py`](../backend/core/database.py)（`SOURCE_TYPES`）、
[`backend/core/ingest_fetchers.py`](../backend/core/ingest_fetchers.py)
（各型別 fetcher）、
[`frontend/lib/sourceTypes.ts`](../frontend/lib/sourceTypes.ts)
（UI 標籤／placeholder）。

---

## 快速對照表

| `source_type` | UI 標籤 | **URL / Identifier** 該填什麼 | 會自動推導 feed？ |
|---|---|---|---|
| `rss` | RSS Feeds | **feed 網址本身**——`https://site.com/feed.xml`（或 `…/feed`、`…/rss`） | ❌ 不會——必須已是 feed 端點 |
| `substack` | Substack Newsletters | `https://<電子報>.substack.com`（填首頁即可） | ✅ 自動補 `/feed` |
| `medium` | Medium Publications | `https://medium.com/@作者` 或 `https://medium.com/feed/<出版品>` | ✅ 自動插入 `/feed`（限 medium.com 站內） |
| `reddit` | Reddit Subscriptions | `https://reddit.com/r/<版名>`（或使用者頁） | ✅ 自動補 `.rss`；遇 403 改用 `old.reddit.com` |
| `youtube_video` | YouTube Subscriptions | 頻道網址、`@handle`、或 `UC…` 頻道 id | ✅ 自動解析成 `feeds/videos.xml` |
| `twitter_tag` | Twitter Tag Feeds | `@帳號`、個人頁網址、或 `#hashtag` | ✅ 透過 Nitter RSS bridge |
| `twitter_article` | Twitter Article Feeds | 同 `twitter_tag` | ✅ 透過同一個 Nitter bridge |
| `patreon` | Patreon Subscriptions | `https://www.patreon.com/rss/<作者>?auth=<token>` | ❌ 貼你自己的會員 RSS 網址（僅音檔） |
| `arxiv` | arXiv Paper Feeds | 查詢字串——`cat:cs.LG`、`au:Name`、`ti:funding+rate`、自由文字、或完整 atom URL | ✅ 透過 arXiv API |
| `glassnode` | Glassnode Insights | `https://research.glassnode.com/rss/` | ❌ 貼 feed 網址 |
| `tiktok` | TikTok Subscriptions | — | — **stub**，永遠不抓（無穩定公開 API） |
| `manual` | Manual Input Feeds | — | — 不輪詢；內容由 **+ INGEST** 按鈕匯入 |

圖例：✅ = 可貼作者首頁／個人頁網址，fetcher 會建出真正的 feed 網址；
❌ = 你必須自己貼一個可用的 feed 網址。

---

## 各型別細節

### `rss` — 通用 RSS / Atom
- **填：** feed 網址，例如 `https://example.com/feed.xml`、`https://example.com/feed`、`https://example.com/index.xml`。
- **行為：** 網址被原封不動交給 feed parser。部落格**首頁**（HTML，不是 feed）
  解不到任何條目，這個來源會顯示錯誤。
- **怎麼找站台的 feed 網址：** 試首頁後面接 `/feed`、`/rss`、`/feed.xml`、
  `/atom.xml`；或檢視網頁原始碼搜 `application/rss+xml` /
  `application/atom+xml`——那裡的 `href` 就是 feed。

### `substack` — Substack 電子報
- **填：** `https://<電子報>.substack.com`（首頁）**或**完整的 `…/feed`。
- **行為：** 若網址結尾不是 `/feed`，fetcher 會自動補上。

### `medium` — Medium 出版品與作者
- **填：** `https://medium.com/@作者`、`https://medium.com/<出版品>`，或完整的 `https://medium.com/feed/<出版品>`。
- **行為：** 當主機是 `medium.com` 且路徑沒有 `/feed` 時，fetcher 會在路徑根部
  插入 `/feed`。**自訂網域**的 Medium 部落格（如 `blog.example.com`）**不會**
  被改寫——那種情況請直接填 feed 網址（通常是 `…/feed`）。

### `reddit` — 子版與使用者 feed
- **填：** `https://reddit.com/r/<版名>` 或使用者頁；結尾 `.rss`/`.json` 可省略。
- **行為：** 缺 `.rss` 時自動補上，送出近似瀏覽器的 User-Agent；若
  `www.reddit.com` 回 HTTP 403，改用 `old.reddit.com` 重試。

### `youtube_video` — 頻道（可選逐字稿）
- **填：** 頻道網址、`@handle`、`UC…` 頻道 id，或現成的 `feeds/videos.xml?channel_id=UC…` 網址。
- **行為：** `@handle` / `/c/` / `/user/` 形式的網址會被 HTML 爬取以解析出
  `UC…` 頻道 id，再轉成頻道 RSS feed。設 `YT_TRANSCRIPT_ENABLED=1` 時，
  每則條目的內文會額外補上影片逐字稿。

### `twitter_tag` / `twitter_article` — 透過 Nitter 抓 X/Twitter
- **填：** `@帳號`、個人頁網址（`https://twitter.com/<帳號>` 或 `x.com`），或 `#hashtag` / 搜尋網址。
- **行為：** 經 Nitter RSS bridge 轉送——帳號變成 `{nitter}/<帳號>/rss`，
  hashtag 變成 `{nitter}/search/rss?q=%23…&f=tweets`。鏡像預設
  `https://nitter.privacydev.net`，可用 `NITTER_INSTANCE_URL` 覆寫（公開鏡像
  常輪替，預期你會需要設定它）。`twitter_article` 與 `twitter_tag` 走完全相同路徑。

### `patreon` — 作者 podcast RSS
- **填：** `https://www.patreon.com/rss/<作者>?auth=<會員token>`。
- **行為：** 網址被原封不動抓取。**兩個硬限制：**
  (1) `?auth=<token>` 是**你個人**的會員 token，位於 Patreon →
  Membership → RSS link，沒帶它 Patreon 會靜默回傳零條目；
  (2) Patreon RSS **只含音檔／podcast**——文字、圖片、影片貼文都抓不到
  （那些需要 OAuth 網頁 API，本系統未實作）。

### `arxiv` — 論文 feed
- **填：** 查詢字串——`cat:cs.LG`（分類）、`au:Cochrane`（作者）、
  `ti:funding+rate`（標題含）、任意自由文字，**或**完整 arXiv atom URL。
- **行為：** 非網址查詢走 arXiv API，依投稿日期由新到舊排序；完整
  `http(s)` atom URL 則當一般 feed 解析。`ARXIV_MAX_RESULTS`
  （預設 `10`，夾在 `1…50`）限制回傳筆數。

### `glassnode` — Glassnode Insights
- **填：** `https://research.glassnode.com/rss/`（或任何 Glassnode 匯出 feed 網址）。
  舊網域 `insights.glassnode.com` 對非瀏覽器 client 會回 Cloudflare 403——請直接用 `research.` 網域。
- **行為：** 當一般 RSS feed 抓取；它有獨立型別，讓 UI 與 KPI 卡片能與通用 `rss` 區分顯示。

### `tiktok` — stub（佔位）
- 一律跳過——沒有穩定的公開 API。加了無害但永不匯入；UI 會標上 **STUB** 徽章。

### `manual` — 操作者手動輸入
- 排程器永不輪詢。URL 留空，改用儀表板上的 **+ INGEST** 對話框推入內容。
  此型別的 cadence 欄位為停用狀態。

---

## 兩種新增來源的方式

1. **快速新增列**（`/sources` 頂端）——貼上任意網址，型別會依主機自動偵測
   （Substack / Medium / Reddit / YouTube / Twitter / arXiv / Glassnode）。任何
   看起來是純 `http(s)` 但認不出主機的網址**預設為 `rss`**（所以在這裡貼首頁
   會建出一個會失敗的 `rss` 來源，除非該網址本身就是 feed）；非 `http(s)` 網址
   則變成 `manual`。cadence 預設 60 分鐘。
2. **+ ADD SOURCE 對話框**——可完整控制 **Name**、**Source Type**、
   **Category**、**URL / Identifier**、**Poll cadence**、**Enabled**。自動偵測
   猜錯、或你想用非預設輪詢間隔時，用這個。

### Cadence、Category、Enabled
- **Poll cadence（輪詢間隔）**——每次輪詢相隔的分鐘數。預設 **60**；對話框接受
  **5** 到 **10080**（7 天）的整數。
- **Category（分類）**——可選的內容分頁（`apps`、`quant_fund`、`research`、
  `ai`…）。留空則依網址自動判斷。
- **Enabled（啟用）**——開啟時，來源會在下一個排程 tick 被輪詢。

---

## 雷區、限制與必要開關

- **排程器總開關。** 只有在 `ALPHA_INGEST_ENABLED=1`（預設 `0`）時來源才會被
  自動輪詢；`ALPHA_INGEST_TICK_SECONDS` 設定 tick 間隔。排程器關著也能新增
  來源——只是在開啟前不會抓取。
- **僅限公開主機（SSRF 守衛）。** `POST /api/sources` 與每次抓取都會拒絕主機名
  解析到私有／loopback／link-local／保留位址的網址。除非設
  `ALPHA_ALLOW_LOCAL_INGEST=1`（僅限本機開發，生產環境絕不要開），否則只有公開
  網際網路網址能用。
- **回應大小上限。** feed 內文有上限（預設 10 MiB，
  `ALPHA_INGEST_MAX_RESPONSE_BYTES`）；超過即拒收。
- **可選的內容擴充。** `ARTICLE_ENRICH_ENABLED=1` 會在 RSS 摘要過短時抓取全文
  （可能違反站台 ToS）；`ASSET_CACHE_ENABLED=1` 會把 feed 內嵌圖片快取到
  `storage/assets/`。
- **去重。** 條目以 `title | url | body` 的內容雜湊去重，重複輪詢同一 feed 不會
  產生重複節點。
