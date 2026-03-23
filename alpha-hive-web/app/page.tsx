'use client';

import { useState } from 'react';

const BG = '#0a0a0f';
const CARD = '#12121a';
const GOLD = '#f0b429';
const TEXT = '#e8e8f0';
const MUTED = '#a0a0b8';
const BORDER = '#1e1e2e';

function HexLogo() {
  return (
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
      <polygon
        points="16,2 28,9 28,23 16,30 4,23 4,9"
        fill="none"
        stroke={GOLD}
        strokeWidth="2"
      />
      <polygon
        points="16,8 22,11.5 22,18.5 16,22 10,18.5 10,11.5"
        fill={GOLD}
        opacity="0.25"
      />
      <circle cx="16" cy="15" r="3" fill={GOLD} />
    </svg>
  );
}

const signals = [
  {
    ticker: 'NVDA',
    score: 8.4,
    direction: '看多',
    dirColor: '#22c55e',
    desc: 'Blackrock 加仓 Form 4 + 期权异动看涨',
  },
  {
    ticker: 'META',
    score: 7.8,
    direction: '看多',
    dirColor: '#22c55e',
    desc: 'Soros Fund 建仓 + AI Capex 催化剂',
  },
  {
    ticker: 'TSLA',
    score: 6.2,
    direction: '中性',
    dirColor: '#f0b429',
    desc: '信号冲突：内部人减持 vs 期权看涨分歧',
  },
];

function ScoreBadge({ score }: { score: number }) {
  const color =
    score >= 7.5 ? '#22c55e' : score >= 6.0 ? GOLD : '#ef4444';
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 10px',
        borderRadius: '999px',
        backgroundColor: `${color}20`,
        border: `1px solid ${color}`,
        color: color,
        fontWeight: 700,
        fontSize: '0.9rem',
        minWidth: '48px',
        textAlign: 'center',
      }}
    >
      {score.toFixed(1)}
    </span>
  );
}

export default function LandingPage() {
  const [email, setEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  async function handleCheckout(e?: React.FormEvent) {
    if (e) e.preventDefault();
    setLoading(true);
    try {
      const res = await fetch('/api/checkout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });
      const data = await res.json();
      if (!res.ok || !data.url) {
        throw new Error(data.error || '无法创建支付会话，请稍后重试。');
      }
      setSubmitted(true);
      window.location.href = data.url;
    } catch (err) {
      const message = err instanceof Error ? err.message : '未知错误';
      alert(`错误：${message}`);
      setLoading(false);
    }
  }

  return (
    <div style={{ backgroundColor: BG, color: TEXT, minHeight: '100vh' }}>
      {/* NAV */}
      <nav
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '1.25rem 2rem',
          borderBottom: `1px solid ${BORDER}`,
          position: 'sticky',
          top: 0,
          backgroundColor: `${BG}e6`,
          backdropFilter: 'blur(12px)',
          zIndex: 50,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
          <HexLogo />
          <span style={{ fontWeight: 700, fontSize: '1.1rem', color: TEXT }}>
            Alpha Hive
          </span>
        </div>
        <a
          href="#pricing"
          style={{
            backgroundColor: GOLD,
            color: BG,
            padding: '0.5rem 1.25rem',
            borderRadius: '6px',
            fontWeight: 700,
            fontSize: '0.9rem',
            textDecoration: 'none',
            cursor: 'pointer',
          }}
        >
          订阅 Pro →
        </a>
      </nav>

      {/* HERO */}
      <section
        style={{
          maxWidth: '860px',
          margin: '0 auto',
          padding: '5rem 2rem 4rem',
          textAlign: 'center',
        }}
      >
        {/* Badge */}
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '0.4rem',
            backgroundColor: `${GOLD}18`,
            border: `1px solid ${GOLD}40`,
            borderRadius: '999px',
            padding: '0.35rem 1rem',
            fontSize: '0.85rem',
            color: GOLD,
            marginBottom: '2rem',
          }}
        >
          <span style={{ fontSize: '0.7rem' }}>●</span>
          每日凌晨自动更新
        </div>

        <h1
          style={{
            fontSize: 'clamp(2.2rem, 5vw, 3.6rem)',
            fontWeight: 800,
            lineHeight: 1.2,
            marginBottom: '1.5rem',
            letterSpacing: '-0.02em',
          }}
        >
          蜂群智能 /{' '}
          <span style={{ color: GOLD }}>每日投资信号</span>
        </h1>

        <p
          style={{
            fontSize: '1.15rem',
            color: MUTED,
            maxWidth: '560px',
            margin: '0 auto 2.5rem',
            lineHeight: '1.75',
          }}
        >
          7 只自治 Agent 并行扫描 SEC 披露、期权异动与 X 情绪，
          每日生成可执行机会简报 — 凌晨推送，盘前就绪。
        </p>

        {/* Email + CTA form */}
        <form
          onSubmit={handleCheckout}
          style={{
            display: 'flex',
            gap: '0.75rem',
            maxWidth: '480px',
            margin: '0 auto',
            flexWrap: 'wrap',
            justifyContent: 'center',
          }}
        >
          <input
            type="email"
            placeholder="输入你的邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            style={{
              flex: '1 1 200px',
              padding: '0.75rem 1rem',
              borderRadius: '8px',
              border: `1px solid ${BORDER}`,
              backgroundColor: CARD,
              color: TEXT,
              fontSize: '1rem',
              outline: 'none',
              minWidth: '0',
            }}
          />
          <button
            type="submit"
            disabled={loading || submitted}
            style={{
              backgroundColor: GOLD,
              color: BG,
              padding: '0.75rem 1.75rem',
              borderRadius: '8px',
              fontWeight: 700,
              fontSize: '1rem',
              border: 'none',
              cursor: loading || submitted ? 'not-allowed' : 'pointer',
              opacity: loading || submitted ? 0.75 : 1,
              display: 'flex',
              alignItems: 'center',
              gap: '0.4rem',
              whiteSpace: 'nowrap',
            }}
          >
            {loading ? (
              <>
                <Spinner />
                跳转中...
              </>
            ) : submitted ? (
              '已提交'
            ) : (
              '开始订阅 →'
            )}
          </button>
        </form>
      </section>

      {/* TRACK RECORD STRIP */}
      <section
        style={{
          borderTop: `1px solid ${BORDER}`,
          borderBottom: `1px solid ${BORDER}`,
          backgroundColor: CARD,
          padding: '1.5rem 2rem',
        }}
      >
        <div
          style={{
            maxWidth: '860px',
            margin: '0 auto',
            display: 'flex',
            justifyContent: 'space-around',
            flexWrap: 'wrap',
            gap: '1.5rem',
          }}
        >
          {[
            { value: '61.3%', label: 'T+7 胜率' },
            { value: '7天', label: '平均持仓' },
            { value: '10个', label: '覆盖标的' },
            { value: '60天', label: '回测样本' },
          ].map((stat) => (
            <div key={stat.label} style={{ textAlign: 'center' }}>
              <div
                style={{ fontSize: '1.75rem', fontWeight: 800, color: GOLD }}
              >
                {stat.value}
              </div>
              <div style={{ fontSize: '0.85rem', color: MUTED, marginTop: '0.25rem' }}>
                {stat.label}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* SIGNAL PREVIEW */}
      <section
        style={{
          maxWidth: '860px',
          margin: '0 auto',
          padding: '4rem 2rem',
        }}
      >
        <h2
          style={{
            fontSize: '1.5rem',
            fontWeight: 700,
            marginBottom: '0.5rem',
            textAlign: 'center',
          }}
        >
          今日信号预览
        </h2>
        <p
          style={{
            color: MUTED,
            textAlign: 'center',
            marginBottom: '2rem',
            fontSize: '0.9rem',
          }}
        >
          示例数据 · Pro 版每日实时更新
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {signals.map((s) => (
            <div
              key={s.ticker}
              style={{
                backgroundColor: CARD,
                border: `1px solid ${BORDER}`,
                borderRadius: '12px',
                padding: '1.25rem 1.5rem',
                display: 'flex',
                alignItems: 'center',
                gap: '1.25rem',
                flexWrap: 'wrap',
              }}
            >
              <div
                style={{
                  fontWeight: 800,
                  fontSize: '1.15rem',
                  minWidth: '60px',
                  color: TEXT,
                }}
              >
                {s.ticker}
              </div>
              <ScoreBadge score={s.score} />
              <span
                style={{
                  color: s.dirColor,
                  fontWeight: 700,
                  fontSize: '0.9rem',
                  minWidth: '36px',
                }}
              >
                {s.direction}
              </span>
              <span style={{ color: MUTED, fontSize: '0.9rem', flex: 1 }}>
                {s.desc}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* PRICING */}
      <section
        id="pricing"
        style={{
          maxWidth: '860px',
          margin: '0 auto',
          padding: '4rem 2rem 6rem',
        }}
      >
        <h2
          style={{
            fontSize: '1.5rem',
            fontWeight: 700,
            textAlign: 'center',
            marginBottom: '2.5rem',
          }}
        >
          选择你的计划
        </h2>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
            gap: '1.5rem',
          }}
        >
          {/* Free tier */}
          <div
            style={{
              backgroundColor: CARD,
              border: `1px solid ${BORDER}`,
              borderRadius: '16px',
              padding: '2rem',
            }}
          >
            <div
              style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: '0.5rem' }}
            >
              免费版
            </div>
            <div
              style={{
                fontSize: '2.5rem',
                fontWeight: 800,
                color: TEXT,
                marginBottom: '1.5rem',
              }}
            >
              $0
              <span style={{ fontSize: '1rem', fontWeight: 400, color: MUTED }}>
                /月
              </span>
            </div>
            <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 2rem', color: MUTED }}>
              {[
                '每周精选 1 条信号',
                '滞后 48 小时推送',
                '仅标的 + 方向',
              ].map((item) => (
                <li
                  key={item}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.5rem',
                    marginBottom: '0.75rem',
                    fontSize: '0.95rem',
                  }}
                >
                  <span style={{ color: MUTED }}>–</span>
                  {item}
                </li>
              ))}
            </ul>
            <button
              disabled
              style={{
                width: '100%',
                padding: '0.75rem',
                borderRadius: '8px',
                backgroundColor: 'transparent',
                border: `1px solid ${BORDER}`,
                color: MUTED,
                fontWeight: 600,
                cursor: 'not-allowed',
                fontSize: '0.95rem',
              }}
            >
              当前计划
            </button>
          </div>

          {/* Pro tier */}
          <div
            style={{
              backgroundColor: CARD,
              border: `2px solid ${GOLD}`,
              borderRadius: '16px',
              padding: '2rem',
              position: 'relative',
            }}
          >
            {/* Recommended badge */}
            <div
              style={{
                position: 'absolute',
                top: '-14px',
                left: '50%',
                transform: 'translateX(-50%)',
                backgroundColor: GOLD,
                color: BG,
                padding: '3px 16px',
                borderRadius: '999px',
                fontSize: '0.8rem',
                fontWeight: 700,
                whiteSpace: 'nowrap',
              }}
            >
              推荐
            </div>

            <div
              style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: '0.5rem' }}
            >
              Pro
            </div>
            <div
              style={{
                fontSize: '2.5rem',
                fontWeight: 800,
                color: GOLD,
                marginBottom: '1.5rem',
              }}
            >
              $49
              <span style={{ fontSize: '1rem', fontWeight: 400, color: MUTED }}>
                /月
              </span>
            </div>
            <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 2rem' }}>
              {[
                '每日 10 个标的完整信号',
                '凌晨实时推送',
                '完整证据链 + 评分',
                '催化剂时间窗 + 失效条件',
                '看空反驳视角',
                '每月自适应权重优化',
              ].map((item) => (
                <li
                  key={item}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.5rem',
                    marginBottom: '0.75rem',
                    fontSize: '0.95rem',
                    color: TEXT,
                  }}
                >
                  <span style={{ color: GOLD }}>✓</span>
                  {item}
                </li>
              ))}
            </ul>
            <button
              onClick={() => handleCheckout()}
              disabled={loading || submitted}
              style={{
                width: '100%',
                padding: '0.85rem',
                borderRadius: '8px',
                backgroundColor: GOLD,
                border: 'none',
                color: BG,
                fontWeight: 700,
                cursor: loading || submitted ? 'not-allowed' : 'pointer',
                opacity: loading || submitted ? 0.75 : 1,
                fontSize: '1rem',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '0.5rem',
              }}
            >
              {loading ? (
                <>
                  <Spinner />
                  跳转到 Stripe...
                </>
              ) : submitted ? (
                '已提交'
              ) : (
                '立即订阅 Pro →'
              )}
            </button>
          </div>
        </div>
      </section>

      {/* FOOTER */}
      <footer
        style={{
          borderTop: `1px solid ${BORDER}`,
          padding: '2rem',
          textAlign: 'center',
          color: '#606080',
          fontSize: '0.8rem',
          lineHeight: '1.8',
        }}
      >
        <p>
          © 2026 Alpha Hive · 以上内容为公开信息研究与情景推演，不构成投资建议。
        </p>
        <p>
          过往表现不代表未来收益。投资有风险，入市需谨慎。
        </p>
      </footer>
    </div>
  );
}

function Spinner() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      style={{ animation: 'spin 0.8s linear infinite' }}
    >
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  );
}
