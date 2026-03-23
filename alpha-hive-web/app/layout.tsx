import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Alpha Hive — 蜂群智能投资信号',
  description: '去中心化蜂群智能，每日自动扫描聪明钱动向，生成结构化投资信号简报。',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body
        style={{
          fontFamily:
            '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans CN", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          backgroundColor: '#0a0a0f',
          color: '#e8e8f0',
          margin: 0,
          padding: 0,
        }}
      >
        {children}
      </body>
    </html>
  );
}
