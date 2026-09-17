/** Login page — redirects to OIDC provider for authentication. */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { PulseIcon } from "@/components/BrandIcons";
import { BrandFooter } from "@/components/BrandFooter";
import { Button, PageHeader } from "@/ui";

export default function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const t = useT();

  useEffect(() => {
    // If already authenticated, redirect to home
    if (isAuthenticated) {
      navigate("/", { replace: true });
    }
  }, [isAuthenticated, navigate]);

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
            <PulseIcon className="text-blue2" size={32} />
          </div>
          <PageHeader
            eyebrow={t("brand.product")}
            title={t("login.heading")}
            className="min-[900px]:grid-cols-1 mb-4 justify-items-center"
          />
          <p className="mb-6 text-[15px] text-muted">{t("login.prompt")}</p>

          <Button variant="primary" onClick={login}>
            {t("login.signIn")}
          </Button>
        </div>
      </div>
      <BrandFooter />
    </div>
  );
}
