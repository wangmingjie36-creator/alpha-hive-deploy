import Link from 'next/link';

export default function CancelPage() {
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
      {/* X icon circle */}
      <div
        style={{
          width: '80px',
          height: '80px',
          borderRadius: '50%',
          backgroundColor: 'rgba(255, 80, 80, 0.1)',
          border: '2px solid #ff5050',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          marginBottom: '2rem',
        }}
      >
        <svg
          width="36"
          height="36"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#ff5050"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
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
        已取消
      </h1>

      <p
        style={{
          fontSize: '1.1rem',
          color: '#a0a0b8',
          maxWidth: '400px',
          lineHeight: '1.7',
          marginBottom: '2.5rem',
        }}
      >
        没关系，你可以随时回来订阅。
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
        }}
      >
        返回首页
      </Link>
    </main>
  );
}
