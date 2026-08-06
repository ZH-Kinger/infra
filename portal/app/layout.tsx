import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host") || "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") || (host.startsWith("localhost") ? "http" : "https");
  const base = new URL(`${protocol}://${host}`);
  const title = "Dataset Ops · 训练数据运维控制台";
  const description = "管理 lakeFS、OSS、CPFS 与 PAI 数据集的发布、运行和生命周期。";
  const image = new URL("/og.png", base).toString();

  return {
    metadataBase: base,
    title,
    description,
    openGraph: { title, description, type: "website", images: [{ url: image, width: 1672, height: 941 }] },
    twitter: { card: "summary_large_image", title, description, images: [image] },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
