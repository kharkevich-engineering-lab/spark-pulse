/** Edit an OCI registry's URL and credentials.
 *
 * The registry's `name` is its identifier and is not sent by `PUT
 * /api/oci/registries/{name}`, so there is no rename field here. A stored
 * token or password is never read back from the registry passed in — only
 * the auth *type* and, for username/password auth, the username are safe to
 * pre-fill. Saving computes a minimal update: the URL only when it changed,
 * and `auth` only when something about it changed, in which case a new
 * secret is included only when the operator actually typed one. The backend
 * merges a partial `auth` update into the stored one rather than replacing
 * it, so leaving the secret field blank keeps the credential already on
 * file. */

import { useEffect, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { Modal } from "@/components/Modal";
import type { OciRegistry, OciRegistryAuthUpdate, OciRegistryUpdate } from "@/lib/types";

type AuthType = "none" | "token" | "username_password";

function authTypeOf(reg: OciRegistry): AuthType {
  return reg.auth?.type ?? reg.auth_type ?? "none";
}

export default function EditRegistryDialog({
  reg,
  onClose,
  onSave,
}: {
  reg: OciRegistry | null;
  onClose: () => void;
  onSave: (name: string, update: OciRegistryUpdate) => Promise<void>;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [authType, setAuthType] = useState<AuthType>("none");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Re-seed the form from the registry being edited every time the dialog
  // opens for one — never from a stored secret, which is not in `reg`.
  useEffect(() => {
    if (!reg) return;
    setUrl(reg.url);
    setAuthType(authTypeOf(reg));
    setUsername(reg.auth?.username ?? "");
    setSecret("");
    setError(null);
    setSaving(false);
  }, [reg]);

  if (!reg) return null;

  const initialAuthType = authTypeOf(reg);
  const initialUsername = reg.auth?.username ?? "";

  const handleSave = async () => {
    const trimmedUrl = url.trim();
    if (!trimmedUrl) {
      setError(t("oci.urlRequired"));
      return;
    }

    if (authType === "username_password" && !username.trim()) {
      setError(t("oci.usernameRequired"));
      return;
    }

    const authChanged =
      authType !== initialAuthType ||
      (authType === "username_password" && username.trim() !== initialUsername) ||
      secret.trim() !== "";

    if (authType !== initialAuthType && authType !== "none" && !secret.trim()) {
      setError(t("oci.secretRequiredOnAuthChange"));
      return;
    }

    const update: OciRegistryUpdate = {};
    if (trimmedUrl !== reg.url) update.url = trimmedUrl;

    if (authChanged) {
      const auth: OciRegistryAuthUpdate = { type: authType };
      if (authType === "username_password") {
        auth.username = username.trim();
        if (secret.trim()) auth.password = secret.trim();
      } else if (authType === "token" && secret.trim()) {
        auth.token = secret.trim();
      }
      update.auth = auth;
    }

    if (Object.keys(update).length === 0) {
      onClose();
      return;
    }

    setError(null);
    setSaving(true);
    try {
      await onSave(reg.name, update);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("oci.registryUpdateFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={!!reg}
      onClose={() => !saving && onClose()}
      title={t("oci.editRegistry", { name: reg.name })}
      actions={
        <>
          <button
            onClick={onClose}
            disabled={saving}
            className="px-4 py-2 rounded-lg border border-border hover:border-border-hover disabled:opacity-50 transition-colors"
          >
            {t("common.cancel")}
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 rounded-lg text-white font-medium bg-primary hover:bg-primary-hover disabled:opacity-50 transition-colors"
          >
            {saving ? t("common.saving") : t("common.save")}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <label className="block text-sm text-text-muted mb-1" htmlFor="edit-registry-url">
            {t("oci.url")}
          </label>
          <input
            id="edit-registry-url"
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
          />
        </div>

        <div>
          <label className="block text-sm text-text-muted mb-1" htmlFor="edit-registry-auth-type">
            {t("oci.authType")}
          </label>
          <select
            id="edit-registry-auth-type"
            value={authType}
            onChange={(e) => setAuthType(e.target.value as AuthType)}
            className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
          >
            <option value="none">{t("oci.authNone")}</option>
            <option value="token">{t("oci.authToken")}</option>
            <option value="username_password">{t("oci.authUsernamePassword")}</option>
          </select>
        </div>

        {authType === "username_password" && (
          <div>
            <label className="block text-sm text-text-muted mb-1" htmlFor="edit-registry-username">
              {t("oci.username")}
            </label>
            <input
              id="edit-registry-username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t("oci.usernamePlaceholder")}
              className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
            />
          </div>
        )}

        {authType !== "none" && (
          <div>
            <label className="block text-sm text-text-muted mb-1" htmlFor="edit-registry-secret">
              {authType === "token" ? t("oci.newToken") : t("oci.newPassword")}
            </label>
            <input
              id="edit-registry-secret"
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              autoComplete="new-password"
              className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
            />
          </div>
        )}

        {error && <p className="text-sm text-danger">{error}</p>}
      </div>
    </Modal>
  );
}
