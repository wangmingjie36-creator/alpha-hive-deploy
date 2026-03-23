import Link from 'next/link';

export default function SuccessPage() {
  return (
    <main
      style={{
        minHeight: '100vh',
        backgroundColor: '#0a0a0f',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '2rem',
        textAlign: 'center',
      }}
    >
      {/* Checkmark circle */}
      <div
        style={{
          width: '80px',
          height: '80px',
          borderRadius: '50%',
          backgroundColor: 'rgba(240, 180, 41, 0.15)',
          border: '2px solid #f0b429',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          marginBottom: '2rem',
        }}
      >
        <svg
          width="40"
          height="40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#f0b429"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </div>

      <h1
        style={{
          fontSize: '2rem',
          fontWeight: 700,
          color: '#e8e8f0',
          marginBottom: '1rem',
        }}
      >
        订阅成功！
      </h1>

      <p
        style={{
          fontSize: '1.1rem',
          color: '#a0a0b8',
          maxWidth: '480px',
          lineHeight: '1.7',
          marginBottom: '2.5rem',
        }}
      >
        欢迎加入 Alpha Hive Pro。你的每日信号将从明天起推送。
      </p>

      <Link
        href="/"
        style={{
          display: 'inline-block',
          backgroundColor: '#f0b429',
          color: '#0a0a0f',
          padding: '0.75rem 2rem',
          borderRadius: '8px',
          fontWeight: 700,
          fontSize: '1rem',
          textDecoration: 'none',
          transition: 'opacity 0.2s',
        }}
      >
        返回首页
      </Link>

      <p
        style={{
          marginTop: '2rem',
          fontSize: '0.85rem',
          color: '#606080',
        }}
      >
        如有问题请联系{' '}
        <a
          href="mailto:support@alphahive.io"
          style={{ color: '#f0b429', textDecoration: 'none' }}
        >
          support@alphahive.io
        </a>
      </p>
    </main>
  );
}
