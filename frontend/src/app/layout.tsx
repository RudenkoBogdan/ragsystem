import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RAG Research Assistant",
  description: "Chat with your scientific papers",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
