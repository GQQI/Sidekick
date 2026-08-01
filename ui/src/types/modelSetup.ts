export type ModelRef = {
  provider_id: string;
  model_id: string;
};

export type ModelEntry = {
  id: string;
  name: string;
  base_url: string;
  api_key?: string;
  api_key_masked?: string;
  api_key_set?: boolean;
};

export type ModelProvider = {
  id: string;
  name: string;
  vendor: string;
  market_id?: string;
  models: ModelEntry[];
};

export type VendorTemplate = {
  name: string;
  vendor: string;
  base_url: string;
  models: string[];
};

export type ModelSetup = {
  version: number;
  providers: ModelProvider[];
  main: ModelRef;
  subagent: ModelRef;
  compress: ModelRef;
  reasoning_effort: string;
  thinking_enabled: boolean;
  temperature: number;
  demo_mode?: boolean;
  vendor_templates?: Record<string, VendorTemplate>;
};

export type ModelRole = "main" | "subagent";

export function refKey(ref: ModelRef): string {
  return `${ref.provider_id}::${ref.model_id}`;
}

export function parseRefKey(key: string): ModelRef | null {
  const i = key.indexOf("::");
  if (i <= 0) return null;
  return { provider_id: key.slice(0, i), model_id: key.slice(i + 2) };
}

export function findModel(
  setup: ModelSetup | null,
  ref: ModelRef | undefined,
): ModelEntry | null {
  if (!setup || !ref?.model_id) return null;
  for (const p of setup.providers) {
    if (p.id !== ref.provider_id) continue;
    const hit = p.models.find((m) => m.id === ref.model_id);
    if (hit) return hit;
  }
  return null;
}

export function modelLabel(
  setup: ModelSetup | null,
  ref: ModelRef | undefined,
): string {
  if (!setup || !ref) return "";
  const prov = setup.providers.find((p) => p.id === ref.provider_id);
  const entry = findModel(setup, ref);
  if (!prov || !entry) return entry?.name || "";
  return `${prov.name} / ${entry.name}`;
}

export function allModelOptions(
  setup: ModelSetup,
  opts?: { requireKey?: boolean },
): Array<{
  key: string;
  provider_id: string;
  provider_name: string;
  vendor: string;
  model_id: string;
  model: string;
  has_key: boolean;
  disabled: boolean;
}> {
  const requireKey = opts?.requireKey ?? false;
  const out: Array<{
    key: string;
    provider_id: string;
    provider_name: string;
    vendor: string;
    model_id: string;
    model: string;
    has_key: boolean;
    disabled: boolean;
  }> = [];
  for (const p of setup.providers) {
    for (const m of p.models) {
      const hasKey = Boolean(m.api_key_set || m.api_key);
      out.push({
        key: refKey({ provider_id: p.id, model_id: m.id }),
        provider_id: p.id,
        provider_name: p.name,
        vendor: p.vendor,
        model_id: m.id,
        model: m.name,
        has_key: hasKey,
        disabled: requireKey && !hasKey,
      });
    }
  }
  return out;
}

export function newModelEntry(
  name: string,
  opts?: { base_url?: string; api_key?: string },
): ModelEntry {
  return {
    id: `mdl_${Math.random().toString(36).slice(2, 10)}`,
    name,
    base_url: opts?.base_url || "",
    api_key: opts?.api_key || "",
  };
}
