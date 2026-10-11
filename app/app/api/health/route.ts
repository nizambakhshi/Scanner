import { guard, read } from "@/lib/api";
import { describeSource } from "@/lib/source";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * `GET /api/health` — the machine-readable form of the health page.
 *
 * `source` states where the numbers came from and which variables are missing, by
 * name; no value is ever included. That is deliberate: a monitoring script should
 * be able to read this endpoint without anyone worrying that it can be used to
 * dump configuration.
 */
export async function GET() {
  const { denied } = await guard();
  if (denied) return denied;
  return read(async () => {
    const [status, workers, coverage, config] = await Promise.all([
      describeSource(),
      source().workerStatus(),
      source().coverage(),
      source().config(),
    ]);
    return {
      ok: status.name === "replay" ? true : (status.database?.ok ?? false),
      source: status,
      workers,
      coverage,
      config: config ? { id: config.id, name: config.name, config_version: config.config_version } : null,
    };
  });
}
