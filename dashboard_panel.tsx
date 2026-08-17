import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { Grid, MetricTile, Pie, TrendChart, Sparkline, Chip, api, type ChartTone } from "@akashic/dashboard-ui";
import type { PluginDispatch } from "akashic-dashboard";

interface UsageCoverage {
  exact: number;
  partial: number;
  unavailable: number;
  legacy: number;
}

interface UsageSummary {
  turns: number;
  errors: number;
  input_tokens: number | null;
  output_tokens: number | null;
  reasoning_output_tokens: number | null;
  cache_prompt_tokens: number | null;
  cache_hit_tokens: number | null;
  cache_miss_tokens: number | null;
  cache_hit_rate: number | null;
  cache_observed_turns: number;
  request_count: number | null;
  covered_request_count: number | null;
  request_coverage_rate: number | null;
  coverage: UsageCoverage;
  avg_iteration: number | null;
  max_iteration: number;
  last_ts: string | null;
}

interface UsageOverview extends UsageSummary {
  schema_version: 2;
  range: string;
  sources: Record<string, UsageSummary>;
}

interface UsagePoint extends UsageSummary {
  bucket: string;
}

interface SourceSeriesPoint {
  bucket: string;
  passive_cache_hit_rate: number | null;
  proactive_cache_hit_rate: number | null;
}

// ── 全局错误（global_errors）类型 ──────────────────────────────────────────────

interface GErrGroup {
  fingerprint: string;
  error_type: string;
  logger_name: string;
  source: string;
  level: string;
  status: string;
  message: string;
  count: number;
  sessions: number;
  channel: string;
  is_new: boolean;
  is_spiking: boolean;
  spark: number[];
  first_ts: string;
  last_ts: string;
}

interface GErrSection {
  key: string;
  label: string;
  count: number;
  items: GErrGroup[];
}

interface GErrListResp {
  range: string;
  facet: string;
  total: number;
  sections: GErrSection[];
}

interface GErrOverview {
  range: string;
  total: number;
  types: number;
  new_types: number;
  spiking_types: number;
  last_ts: string | null;
  spark: number[];
}

interface GErrVariant {
  fingerprint: string;
  count: number;
  traceback_text: string;
}

interface GErrOccurrence {
  session_key: string;
  ts: string | null;
  user_preview: string;
}

interface GErrDetail {
  fingerprint: string;
  error_type: string;
  logger_name: string;
  source: string;
  level: string;
  status: string;
  message: string;
  traceback_text: string;
  count: number;
  sessions: number;
  channel: string;
  first_ts: string;
  last_ts: string;
  trend: { bucket: string; count: number }[];
  variants: GErrVariant[];
  occurrences: GErrOccurrence[];
}

const RANGES: { key: string; label: string }[] = [
  { key: "24h", label: "24 小时" },
  { key: "7d", label: "7 天" },
  { key: "30d", label: "30 天" },
  { key: "90d", label: "90 天" },
  { key: "all", label: "全部" },
];

const SOURCE_LABEL: Record<string, string> = {
  log: "主动日志",
  uncaught: "未捕获异常",
  asyncio: "asyncio 任务",
  thread: "子线程",
};

const STATUS_META: Record<string, { label: string; tone: ChartTone }> = {
  active: { label: "活跃", tone: "warning" },
  acknowledged: { label: "已确认", tone: "muted" },
  ignored: { label: "已忽略", tone: "success" },
};

const TONE_BG: Record<ChartTone, string> = {
  danger: "bg-danger",
  warning: "bg-warning",
  success: "bg-success",
  accent: "bg-accent",
  muted: "bg-subtle",
};

function _compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

function _compactKnown(value: number | null): string {
  return value == null ? "—" : _compact(value);
}

function _pct(value: number | null): string {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

function _coverageLabel(coverage: UsageCoverage): string {
  const parts = [`精确 ${coverage.exact}`];
  if (coverage.partial > 0) parts.push(`部分 ${coverage.partial}`);
  if (coverage.unavailable > 0) parts.push(`不可用 ${coverage.unavailable}`);
  if (coverage.legacy > 0) parts.push(`旧记录 ${coverage.legacy}`);
  return parts.join(" · ");
}

function _combinedCacheRate(...summaries: Array<UsageSummary | undefined>): number | null {
  const observed = summaries.filter(
    (summary): summary is UsageSummary => summary?.cache_prompt_tokens != null && summary.cache_hit_tokens != null,
  );
  if (observed.length === 0) return null;
  const prompt = observed.reduce((total, summary) => total + (summary.cache_prompt_tokens ?? 0), 0);
  const hit = observed.reduce((total, summary) => total + (summary.cache_hit_tokens ?? 0), 0);
  return prompt > 0 ? hit / prompt : null;
}

function _knownTrend<T extends { bucket: string }>(
  points: T[],
  valueOf: (point: T) => number | null,
  scale = 1,
): Array<{ label: string; value: number }> {
  return points.flatMap((point) => {
    const value = valueOf(point);
    return value == null
      ? []
      : [{ label: _bucketLabel(point.bucket), value: value * scale }];
  });
}

// Shorten an ISO-bucket label: "2026-06-17T11" -> "11:00", "2026-06-17" -> "6-17".
function _bucketLabel(bucket: string): string {
  if (bucket.includes("T")) return `${bucket.slice(11, 13)}:00`;
  const [, m, d] = bucket.split("-");
  return m && d ? `${Number(m)}-${d}` : bucket;
}

function _shortTs(value: string | null): string {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value || "—";
  return `${dt.getMonth() + 1}-${String(dt.getDate()).padStart(2, "0")} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
}

// Percentage change of the last bucket vs the previous one, for the tile delta.
function _delta(values: number[]): number | null {
  if (values.length < 2) return null;
  const last = values[values.length - 1];
  const prev = values[values.length - 2];
  if (!prev) return null;
  return ((last - prev) / prev) * 100;
}

// 相对时间标签："刚刚" / "Xs 前" / "Xm 前"。
function _ago(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "刚刚";
  const s = Math.floor(ms / 1000);
  if (s < 3) return "刚刚";
  if (s < 60) return `${s}s 前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m 前`;
  return `${Math.floor(m / 60)}h 前`;
}

// 严重度配色：爆发或高频 -> danger，中频 -> warning，低频 -> muted。
function _severity(count: number, spiking: boolean): ChartTone {
  if (spiking || count >= 20) return "danger";
  if (count >= 5) return "warning";
  return "muted";
}

function Card({ title, children, bodyClass, style }: { title: string; children: ReactNode; bodyClass?: string; style?: React.CSSProperties }): ReactElement {
  return (
    <div
      className="flex flex-col overflow-hidden border border-border bg-surface"
      style={style}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h3 className="text-[12px] font-medium text-muted">{title}</h3>
      </div>
      <div className={bodyClass ?? "p-4"}>{children}</div>
    </div>
  );
}

// ── 错误排障台：从「错误」KPI 卡 FLIP 放大的独立卡片，stats-over-detail 构图 ──────────

function ErrorDrill({
  portalRef,
  range,
  onClose,
}: {
  portalRef: React.RefObject<HTMLButtonElement | null>;
  range: string;
  onClose: () => void;
}): ReactElement {
  const drillRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [overview, setOverview] = useState<GErrOverview | null>(null);
  const [facet, setFacet] = useState<string>("type");
  const [q, setQ] = useState<string>("");
  const [sections, setSections] = useState<GErrSection[]>([]);
  const [selFp, setSelFp] = useState<string | null>(null);
  const [detail, setDetail] = useState<GErrDetail | null>(null);
  const [tab, setTab] = useState<"trend" | "trace" | "occ">("trace");
  const [variant, setVariant] = useState<number>(0);

  const loadList = useCallback(async () => {
    const [ov, list] = await Promise.all([
      api<GErrOverview>(`/api/dashboard/observe/global_errors/overview?range=${range}`),
      api<GErrListResp>(`/api/dashboard/observe/global_errors?range=${range}&facet=${facet}&q=${encodeURIComponent(q)}`),
    ]);
    setOverview(ov);
    setSections(list.sections ?? []);
    const flat = (list.sections ?? []).flatMap((s) => s.items);
    setSelFp((cur) => (cur && flat.some((i) => i.fingerprint === cur) ? cur : flat[0]?.fingerprint ?? null));
  }, [range, facet, q]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    if (!selFp) {
      setDetail(null);
      return;
    }
    let alive = true;
    void (async () => {
      const d = await api<GErrDetail>(`/api/dashboard/observe/global_errors/${selFp}?range=${range}`);
      if (alive) {
        setDetail(d);
        setVariant(0);
        setTab("trace");
      }
    })();
    return () => {
      alive = false;
    };
  }, [selFp, range]);

  const close = useCallback(() => {
    onClose();
  }, [onClose]);

  useEffect(() => {
    closeButtonRef.current?.focus();
    return () => portalRef.current?.focus();
  }, [portalRef]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") close();
      if (e.key !== "Tab") return;
      const focusable = Array.from(
        drillRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  const setStatus = async (status: string): Promise<void> => {
    if (!detail) return;
    await api(`/api/dashboard/observe/global_errors/${detail.fingerprint}/status?value=${status}`, { method: "POST" });
    await loadList();
    setDetail((d) => (d ? { ...d, status } : d));
  };

  const gotoSession = (key: string): void => {
    window.dispatchEvent(new CustomEvent("akashic:goto-session", { detail: key }));
    close();
  };

  return (
    <>
      <div aria-hidden="true" className="fixed inset-0 z-30 bg-black/55" onClick={close} />
      <div
        ref={drillRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="observe-error-dialog-title"
        className="fixed z-40 flex flex-col overflow-hidden rounded-md border border-border-strong bg-surface"
        style={{
          width: "min(1180px, 94vw)",
          height: "min(84vh, 760px)",
          left: "50%",
          top: "50%",
          marginLeft: "calc(min(1180px, 94vw) / -2)",
          marginTop: "calc(min(84vh, 760px) / -2)",
        }}
      >
        {/* 头部：402 大数字 + 摘要徽标 + range */}
        <div className="flex flex-shrink-0 items-center gap-4 border-b border-border px-5 py-4">
          <button
            ref={closeButtonRef}
            type="button"
            onClick={close}
            className="grid h-10 w-10 place-items-center rounded-md border border-border-strong bg-surface-2 text-[18px] text-muted transition-colors hover:text-fg"
            aria-label="关闭错误分析"
            title="返回 (Esc)"
          >
            ‹
          </button>
          <span className="font-mono text-[26px] font-semibold tabular-nums text-danger">{overview?.total ?? "—"}</span>
          <div className="min-w-0">
            <div id="observe-error-dialog-title" className="text-sm font-semibold">错误 · {RANGES.find((r) => r.key === range)?.label ?? range}</div>
            <div className="mt-0.5 flex items-center gap-3 text-[11px] text-muted">
              <span>{overview?.types ?? 0} 个类型</span>
              {(overview?.new_types ?? 0) > 0 && <span>{overview?.new_types} 个新类型</span>}
              {(overview?.spiking_types ?? 0) > 0 && <span className="font-semibold text-danger">{overview?.spiking_types} 个正在爆发</span>}
            </div>
          </div>
        </div>

        {/* 切维 + 搜索 */}
        <div className="flex flex-shrink-0 items-center gap-3 border-b border-border px-4 py-2.5">
          <div className="flex gap-1 rounded-md border border-border bg-bg p-0.5">
            {[
              { k: "type", l: "按类型" },
              { k: "source", l: "按来源" },
              { k: "channel", l: "按通道" },
            ].map((f) => (
              <button
                key={f.k}
                type="button"
                onClick={() => setFacet(f.k)}
                className={`rounded-[4px] px-2.5 py-1 text-[11px] transition-colors ${facet === f.k ? "bg-surface-3 text-fg" : "text-muted hover:text-fg"}`}
              >
                {f.l}
              </button>
            ))}
          </div>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            aria-label="搜索错误"
            placeholder="按消息 / 模块过滤…"
            className="w-[280px] rounded-md border border-border bg-bg px-3 py-1.5 text-[11.5px] text-fg outline-none focus:border-accent-deep"
          />
        </div>

        {/* 左群组 / 右详情 */}
        <div className="grid min-h-0 flex-1 grid-cols-[340px_1fr]">
          <div className="overflow-auto border-r border-border p-1.5">
            {sections.map((section) => (
              <div key={section.key}>
                {section.label && (
                  <div className="flex items-center justify-between px-2.5 pb-1 pt-3 text-[11px] font-medium text-muted">
                    <span>{section.label}</span>
                    <span>{section.count} 次</span>
                  </div>
                )}
                {section.items.map((g) => (
                  <ErrorRow key={g.fingerprint} g={g} active={g.fingerprint === selFp} onClick={() => setSelFp(g.fingerprint)} />
                ))}
              </div>
            ))}
            {sections.length === 0 && <div className="p-6 text-[12.5px] text-muted">所选区间内没有错误。</div>}
          </div>

          {detail ? (
            <ErrorDetail
              detail={detail}
              tab={tab}
              setTab={setTab}
              variant={variant}
              setVariant={setVariant}
              onStatus={setStatus}
              onGoto={gotoSession}
            />
          ) : (
            <div className="grid place-items-center text-[13px] text-muted">选择左侧一个错误查看现场</div>
          )}
        </div>
      </div>
    </>
  );
}

function ErrorRow({ g, active, onClick }: { g: GErrGroup; active: boolean; onClick: () => void }): ReactElement {
  const tone = _severity(g.count, g.is_spiking);
  const spark = g.spark ?? [];
  return (
    <button
      type="button"
      onClick={onClick}
      className={`grid w-full grid-cols-[9px_1fr_auto] items-center gap-2.5 border-b border-border px-3 py-2.5 text-left transition-colors duration-150 ${active ? "bg-accent-soft" : "hover:bg-surface-2"}`}
    >
      <span className="relative flex h-2 w-2" aria-hidden="true">
        <span className={`relative inline-flex h-2 w-2 rounded-full ${TONE_BG[tone]}`} />
      </span>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 font-mono text-[12.5px]">
          <span className="truncate">{g.error_type}</span>
          {g.is_new && <span className="text-[9px] font-semibold text-accent">新</span>}
          {g.is_spiking && <span className="text-[9px] font-semibold text-danger">爆发</span>}
        </div>
        <div className="mt-0.5 truncate font-mono text-[10px] text-subtle">{g.logger_name}</div>
        <div className="mt-1 flex gap-2.5 text-[10px] text-muted">
          <span><b className="font-semibold text-fg">{g.count}</b> 次</span>
          <span><b className="font-semibold text-fg">{g.sessions}</b> session</span>
        </div>
      </div>
      <div className="flex flex-col items-end gap-1.5">
        <div className="h-[22px] w-[62px]">{spark.length > 1 && <Sparkline data={spark} tone={tone} height={22} />}</div>
        <span className="font-mono text-[10px] text-subtle">{_shortTs(g.last_ts)}</span>
      </div>
    </button>
  );
}

function ErrorDetail({
  detail,
  tab,
  setTab,
  variant,
  setVariant,
  onStatus,
  onGoto,
}: {
  detail: GErrDetail;
  tab: "trend" | "trace" | "occ";
  setTab: (t: "trend" | "trace" | "occ") => void;
  variant: number;
  setVariant: (n: number) => void;
  onStatus: (s: string) => void;
  onGoto: (key: string) => void;
}): ReactElement {
  const status = STATUS_META[detail.status] ?? STATUS_META.active;
  const tone = _severity(detail.count, false);
  const activeVariant = detail.variants[variant] ?? detail.variants[0];
  return (
    <div className="flex min-h-0 flex-col">
      {/* hero */}
      <div className="border-b border-border px-5 py-4">
        <div className="font-mono text-[19px] font-semibold">{detail.error_type}</div>
        <div className="mt-1.5 font-mono text-[12px] leading-relaxed text-danger">{detail.message}</div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Chip>{detail.logger_name}</Chip>
          <Chip>来源 · {SOURCE_LABEL[detail.source] ?? detail.source}</Chip>
          <Chip>{detail.channel}</Chip>
          <Chip tone="danger">{detail.level}</Chip>
          <Chip tone={status.tone}>{status.label}</Chip>
        </div>
      </div>

      {/* 爆炸半径 */}
      <div className="grid grid-cols-4 gap-px border-b border-border bg-border">
        <Blast label="累计次数" value={String(detail.count)} />
        <Blast label="独立 session" value={String(detail.sessions)} />
        <Blast label="首次" value={_shortTs(detail.first_ts)} small />
        <Blast label="最近" value={_shortTs(detail.last_ts)} small />
      </div>

      {/* 分段 */}
      <div className="flex gap-1 border-b border-border px-5 pt-3">
        <TabBtn active={tab === "trend"} onClick={() => setTab("trend")}>趋势</TabBtn>
        <TabBtn active={tab === "trace"} onClick={() => setTab("trace")}>
          Traceback{detail.variants.length > 1 ? ` · ${detail.variants.length} 变体` : ""}
        </TabBtn>
        <TabBtn active={tab === "occ"} onClick={() => setTab("occ")}>现场 · {detail.occurrences.length}</TabBtn>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {tab === "trend" && (
          <TrendChart
            data={detail.trend.map((p) => ({ label: _bucketLabel(p.bucket), value: p.count }))}
            kind="bar"
            tone={tone}
            valueFmt={(n) => String(n)}
            empty="区间内无发作"
          />
        )}
        {tab === "trace" && (
          <div>
            {detail.variants.length > 1 && (
              <div className="mb-3 flex gap-2">
                {detail.variants.map((v, i) => (
                  <button
                    key={v.fingerprint}
                    type="button"
                    onClick={() => setVariant(i)}
                    className={`rounded border px-2.5 py-1.5 text-left text-[10.5px] ${i === variant ? "border-accent-deep bg-accent-soft text-fg" : "border-border bg-bg text-muted"}`}
                  >
                    <b className="text-fg">{v.count}</b> 次 · 变体 {i + 1}
                  </button>
                ))}
              </div>
            )}
            <pre className="m-0 max-h-[280px] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-muted">
              {activeVariant?.traceback_text || detail.traceback_text}
            </pre>
          </div>
        )}
        {tab === "occ" && (
          <div className="flex flex-col gap-2">
            {detail.occurrences.length === 0 && <div className="text-[12px] text-muted">无可关联的 session 现场。</div>}
            {detail.occurrences.map((o) => (
              <div key={o.session_key} className="grid grid-cols-[auto_1fr_auto] items-center gap-3.5 border-b border-border bg-bg px-3.5 py-2.5">
                <span className="font-mono text-[11px] text-accent">{_shortTs(o.ts)}</span>
                <div className="min-w-0">
                  <div className="truncate text-[12px]">{o.user_preview || "（无用户消息）"}</div>
                  <div className="mt-0.5 font-mono text-[10px] text-subtle">session {o.session_key}</div>
                </div>
                <button
                  type="button"
                  onClick={() => onGoto(o.session_key)}
                  className="whitespace-nowrap rounded border border-accent-deep bg-accent-soft px-2.5 py-1.5 text-[10.5px] text-accent-ink"
                >
                  查看对话
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 操作 */}
      <div className="flex flex-shrink-0 gap-2 border-t border-border px-5 py-3">
        <button
          type="button"
          onClick={() => detail.occurrences[0] && onGoto(detail.occurrences[0].session_key)}
          disabled={detail.occurrences.length === 0}
          className="rounded border border-accent-deep bg-accent-soft px-3 py-2 text-[11px] text-accent-ink transition-colors disabled:opacity-40"
        >
          查看最近对话
        </button>
        <button
          type="button"
          onClick={() => void navigator.clipboard?.writeText(detail.traceback_text)}
          className="rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:text-fg"
        >
          复制 Traceback
        </button>
        <div className="flex-1" />
        <button type="button" onClick={() => onStatus("acknowledged")} className="rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:text-fg">
          标记已确认
        </button>
        <button type="button" onClick={() => onStatus("ignored")} className="rounded border border-border-strong bg-surface-2 px-3 py-2 text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger">
          忽略此类型
        </button>
      </div>
    </div>
  );
}

function Blast({ label, value, small }: { label: string; value: string; small?: boolean }): ReactElement {
  return (
    <div className="bg-surface px-4 py-3">
      <div className="text-[10px] text-subtle">{label}</div>
      <div className={`mt-1.5 font-mono font-semibold tabular-nums ${small ? "text-[12.5px]" : "text-[18px]"}`}>{value}</div>
    </div>
  );
}

function TabBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`-mb-px border-b-2 px-3 py-2 text-[11.5px] transition-colors ${active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg"}`}
    >
      {children}
    </button>
  );
}

// 首屏骨架保留稳定结构，避免加载时布局跳动。
function SkelBlock({ className }: { className: string }): ReactElement {
  return (
    <div className={`relative overflow-hidden rounded border border-border bg-surface-2 ${className}`} />
  );
}

function ObserveSkeleton(): ReactElement {
  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex items-end justify-between">
        <div className="flex flex-col gap-2">
          <SkelBlock className="h-7 w-48" />
          <SkelBlock className="h-3 w-64 rounded" />
        </div>
        <SkelBlock className="h-9 w-56" />
      </div>
      <div className="grid grid-cols-4 gap-4">
        {[0, 1, 2, 3].map((i) => <SkelBlock key={i} className="h-[132px]" />)}
      </div>
      <div className="grid grid-cols-2 gap-4">
        {[0, 1, 2, 3].map((i) => <SkelBlock key={i} className="h-[218px]" />)}
      </div>
    </div>
  );
}

// ── 监测主面板 ────────────────────────────────────────────────────────────────

// Grafana-style monitoring overview over observe.db agent-loop telemetry.
function ObserveMain(_props: { dispatch: PluginDispatch }): ReactElement {
  const [range, setRange] = useState<string>("24h");
  const [overview, setOverview] = useState<UsageOverview | null>(null);
  const [points, setPoints] = useState<UsagePoint[]>([]);
  const [sourcePoints, setSourcePoints] = useState<SourceSeriesPoint[]>([]);
  const [gErr, setGErr] = useState<GErrOverview | null>(null);
  const [drillOpen, setDrillOpen] = useState<boolean>(false);
  const [updatedAt, setUpdatedAt] = useState<number>(0);
  const [nowTs, setNowTs] = useState<number>(() => Date.now());
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const portalRef = useRef<HTMLButtonElement>(null);
  const overviewRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const [ov, series, sourceSeries, ge] = await Promise.all([
        api<UsageOverview>(`/api/dashboard/observe/v2/overview?range=${range}`),
        api<{ points: UsagePoint[] }>(`/api/dashboard/observe/v2/timeseries?range=${range}`),
        api<{ points: SourceSeriesPoint[] }>(`/api/dashboard/observe/timeseries?range=${range}`),
        api<GErrOverview>(`/api/dashboard/observe/global_errors/overview?range=${range}`),
      ]);
      setOverview(ov);
      setPoints(series.points ?? []);
      setSourcePoints(sourceSeries.points ?? []);
      setGErr(ge);
      setUpdatedAt(Date.now());
    } finally {
      setRefreshing(false);
    }
  }, [range]);

  // 首次 + range 变化加载；并以 15s 心跳自动刷新，制造"活的"实时感。
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(id);
  }, [load]);

  // 1s tick 驱动"更新于 Xs 前"的相对时间标签。
  useEffect(() => {
    const id = window.setInterval(() => setNowTs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (overviewRef.current) overviewRef.current.inert = drillOpen;
  }, [drillOpen]);

  if (!overview) {
    return <ObserveSkeleton />;
  }

  const turnSeries = points.map((p) => p.turns);
  const errorSeries = points.map((p) => p.errors);
  const inputTrend = _knownTrend(points, (p) => p.input_tokens);
  const outputTrend = _knownTrend(points, (p) => p.output_tokens);
  const coverageTrend = _knownTrend(points, (p) => p.request_coverage_rate, 100);
  const passiveHitTrend = _knownTrend(sourcePoints, (p) => p.passive_cache_hit_rate, 100);
  const proactiveHitTrend = _knownTrend(sourcePoints, (p) => p.proactive_cache_hit_rate, 100);
  const iterTrend = _knownTrend(points, (p) => p.avg_iteration);
  const inputSeries = inputTrend.map((p) => p.value);
  const outputSeries = outputTrend.map((p) => p.value);
  const coverageSeries = coverageTrend.map((p) => p.value);
  const passiveHitSeries = passiveHitTrend.map((p) => p.value);
  const iterSeries = iterTrend.map((p) => p.value);
  const labelled = (vals: number[]) => points.map((p, i) => ({ label: _bucketLabel(p.bucket), value: vals[i] }));
  const passive = overview.sources.agent;
  const proactiveRate = _combinedCacheRate(overview.sources.proactive, overview.sources.drift);
  const cacheTone: ChartTone = overview.cache_hit_rate == null ? "muted" : "success";
  const coverageTone: ChartTone = overview.request_coverage_rate == null
    ? "muted"
    : overview.coverage.partial > 0 || overview.coverage.unavailable > 0 || overview.coverage.legacy > 0
      ? "warning"
      : "success";

  // 全局错误总数（采集到的、跨子系统）优先用于「错误」卡，与排障台数字一致。
  const gErrTotal = gErr?.total ?? overview.errors;
  return (
    <>
      <div
        ref={overviewRef}
        aria-hidden={drillOpen || undefined}
        className="flex flex-col gap-5 p-6 transition-opacity duration-150"
        style={drillOpen ? { opacity: 0.35, pointerEvents: "none" } : undefined}
      >
        {/* header + range switcher */}
        <div className="flex items-end justify-between">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="detail-title">Roxy Observe · 监测</span>
              <span className="text-[11px] font-medium text-success">实时更新</span>
            </div>
            <div className="detail-subtext">
              Roxy 主循环遥测 · Token / 缓存 / 覆盖 / 错误
              <span className="ml-2 font-mono text-[11px] text-subtle">更新于 {_ago(nowTs - updatedAt)}</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void load()}
              className={`grid h-10 w-10 place-items-center rounded-md border border-border bg-surface-2 text-muted transition-colors hover:border-border-strong hover:text-fg ${refreshing ? "animate-spin" : ""}`}
              aria-label="刷新监测数据"
              title="刷新"
            >
              ↻
            </button>
            <div className="flex gap-1 rounded-md border border-border bg-surface-2 p-1">
              {RANGES.map((r) => (
                <button
                  key={r.key}
                  type="button"
                  onClick={() => setRange(r.key)}
                  className={`min-h-10 rounded-[4px] px-2.5 py-1 text-[11px] transition-colors ${range === r.key ? "bg-accent text-accent-ink" : "text-muted hover:bg-surface-3 hover:text-fg"}`}
                  aria-pressed={range === r.key}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <section
          className={`flex min-h-16 items-center justify-between gap-4 border px-4 py-3 ${gErrTotal > 0 ? "border-danger/40 bg-danger/10" : "border-success/35 bg-success/10"}`}
          aria-label="运行状态"
        >
          <div className="min-w-0">
            <div className={`text-[13px] font-semibold ${gErrTotal > 0 ? "text-danger" : "text-success"}`}>
              {gErrTotal > 0 ? `${gErrTotal} 条错误需要查看` : "当前区间没有采集到错误"}
            </div>
            <p className="mt-1 text-[11.5px] text-muted">
              {gErrTotal > 0 ? `${gErr?.types ?? 0} 个错误类型，先查看爆发和新出现的类型。` : "主循环遥测持续更新，缓存与迭代指标见下方。"}
            </p>
          </div>
          {gErrTotal > 0 && (
            <button
              type="button"
              ref={portalRef}
              onClick={() => setDrillOpen(true)}
              className="min-h-10 flex-shrink-0 rounded-md border border-danger/40 bg-surface px-3 text-[12px] font-semibold text-danger hover:bg-danger/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              查看错误分析
            </button>
          )}
        </section>

        {/* 模型用量指标 */}
        <Grid columns={4}>
          <div>
            <MetricTile label="缓存命中率" value={_pct(overview.cache_hit_rate)} sub={`${_compactKnown(overview.cache_hit_tokens)} 命中 · ${_compactKnown(overview.cache_miss_tokens)} 未命中`} tone={cacheTone} />
          </div>
          <div>
            <MetricTile label="输入 Token" value={_compactKnown(overview.input_tokens)} sub={`${overview.cache_observed_turns} 轮具有缓存明细`} tone="accent" spark={inputSeries} />
          </div>
          <div>
            <MetricTile label="输出 Token" value={_compactKnown(overview.output_tokens)} sub={`推理 ${_compactKnown(overview.reasoning_output_tokens)}`} tone="accent" spark={outputSeries} />
          </div>
          <div>
            <MetricTile label="请求覆盖率" value={_pct(overview.request_coverage_rate)} sub={_coverageLabel(overview.coverage)} tone={coverageTone} spark={coverageSeries} />
          </div>
        </Grid>

        {/* 关键运行指标 */}
        <Grid columns={3}>
          <div>
            <MetricTile label="对话轮数" value={_compact(overview.turns)} delta={_delta(turnSeries)} sub={overview.last_ts ? `最近 ${_shortTs(overview.last_ts)}` : "无记录"} tone="accent" spark={turnSeries} />
          </div>
          <div>
            <MetricTile label="被动 KV 命中率" value={_pct(passive?.cache_hit_rate ?? null)} sub={`主动 ${_pct(proactiveRate)}`} tone="success" spark={passiveHitSeries} />
          </div>
          <div>
            <MetricTile label="平均迭代" value={overview.avg_iteration != null ? overview.avg_iteration.toFixed(1) : "—"} unit={`峰 ${overview.max_iteration}`} sub="每轮 LLM 调用次数" tone="warning" spark={iterSeries} />
          </div>
        </Grid>

        {/* 首要趋势 */}
        <Grid columns={2}>
          <Card title="输入 Token 趋势">
            <TrendChart data={inputTrend} kind="area" tone="accent" valueFmt={_compact} empty="暂无已报告输入用量" />
          </Card>
          <Card title="输出 Token 趋势">
            <TrendChart data={outputTrend} kind="area" tone="accent" valueFmt={_compact} empty="暂无已报告输出用量" />
          </Card>
        </Grid>

        <Grid columns={2}>
          <Card title="缓存构成">
            {overview.cache_prompt_tokens != null && overview.cache_prompt_tokens > 0 && overview.cache_hit_tokens != null && overview.cache_miss_tokens != null ? (
              <Pie rate={overview.cache_hit_rate} hit={overview.cache_hit_tokens} miss={overview.cache_miss_tokens} />
            ) : (
              <div className="flex h-[170px] items-center justify-center text-[12px] text-subtle">暂无缓存明细</div>
            )}
          </Card>
          <Card title="请求覆盖率趋势">
            <TrendChart data={coverageTrend} kind="area" tone="success" valueFmt={(n) => `${n.toFixed(0)}%`} empty="暂无请求覆盖数据" />
          </Card>
        </Grid>

        <details className="border-t border-border pt-1">
          <summary className="min-h-11 cursor-pointer py-3 text-[12px] font-semibold text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
            查看缓存命中与错误趋势
          </summary>
          <Grid columns={2}>
            <Card title="全局被动链路命中率趋势">
              <TrendChart data={passiveHitTrend} kind="area" tone="success" valueFmt={(n) => `${n.toFixed(0)}%`} empty="暂无被动链路缓存数据" />
            </Card>
            <Card title="全局主动链路命中率趋势">
              <TrendChart data={proactiveHitTrend} kind="area" tone="accent" valueFmt={(n) => `${n.toFixed(0)}%`} empty="暂无主动链路缓存数据" />
            </Card>
            <Card title="错误趋势">
              <TrendChart data={labelled(errorSeries)} kind="bar" tone="danger" valueFmt={(n) => String(n)} empty="所选区间内没有错误" />
            </Card>
          </Grid>
        </details>
      </div>

      {drillOpen && <ErrorDrill portalRef={portalRef} range={range} onClose={() => setDrillOpen(false)} />}
    </>
  );
}

window.AkashicDashboard.registerPlugin({
  id: "observe",
  label: "运行监测",
  viewLabel: "运行监测",
  layout: "workbench",
  pageSize: 30,
  rowKey: "id",

  countTitle(total: number): string {
    return `${total} 轮遥测`;
  },

  columns: [
    { key: "session_key", label: "会话", width: 120, cellClass: "mono cell-session", rawTitle: true },
    { key: "ts", label: "时间", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "error", label: "错误", flex: true, cellClass: "content-preview" },
  ],

  async getCount(): Promise<number | null> {
    try {
      const ov = await api<UsageOverview>("/api/dashboard/observe/v2/overview?range=all");
      return ov.turns || 0;
    } catch {
      return null;
    }
  },

  async fetchPage({ page, pageSize }: { page: number; pageSize: number }) {
    const data = await api<{ items: Record<string, unknown>[]; total: number }>(
      `/api/dashboard/observe/errors?range=all&page=${page}&page_size=${pageSize}`,
    );
    return { items: data.items || [], total: data.total || 0 };
  },

  Main: ObserveMain,
});
