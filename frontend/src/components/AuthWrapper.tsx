"use client";
import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { isAuthenticated } from "@/lib/auth";

export default function AuthWrapper({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [isReady, setIsReady] = useState(false);

  useEffect(() => {
    const auth = isAuthenticated();
    const isAuthPage = pathname === "/login" || pathname === "/register";

    if (auth && isAuthPage) {
      router.replace("/chat");
    } else if (!auth && pathname !== "/login" && pathname !== "/register") {
      router.replace("/login");
    } else {
      setIsReady(true);
    }
  }, [pathname, router]);

  // Показать loading только если идёт редирект
  if (!isReady) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg-primary">
        <div className="text-text-secondary text-sm">Loading...</div>
      </div>
    );
  }

  return <>{children}</>;
}
