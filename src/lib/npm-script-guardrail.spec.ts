import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

interface LockfilePackageEntry {
  name?: string;
  version?: string;
  resolved?: string;
  optional?: boolean;
  os?: string[];
  cpu?: string[];
  libc?: string[];
  inBundle?: boolean;
  link?: boolean;
  hasInstallScript?: boolean;
  scripts?: Record<string, string>;
}

interface PackageLockfile {
  packages?: Record<string, LockfilePackageEntry>;
}

interface DockerfileStage {
  name: string;
  lines: string[];
}

const EXACT_SEMVER_RE =
  /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const PACKAGE_NAME_RE = /^(?:@[a-z0-9-~][a-z0-9-._~]*\/)?[a-z0-9-~][a-z0-9-._~]*$/;

function readRepoFile(relativePathFromLib: string): string {
  return readFileSync(fileURLToPath(new URL(relativePathFromLib, import.meta.url)), "utf8");
}

function parseNpmrc(source: string): Map<string, string> {
  const entries = new Map<string, string>();
  for (const rawLine of source.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const eqIndex = line.indexOf("=");
    if (eqIndex === -1) continue;
    const key = line.slice(0, eqIndex).trim();
    const value = line.slice(eqIndex + 1).trim();
    entries.set(key, value);
  }
  return entries;
}

function parseAllowScriptsKey(
  rawKey: string,
): { name: string; versions: Set<string> | null } | null {
  const key = rawKey.trim();
  if (!key || key !== rawKey || /^\d+$/.test(key) || key.startsWith("npm:")) {
    return null;
  }

  const specSeparator = key.startsWith("@") ? key.indexOf("@", 1) : key.indexOf("@");
  if (specSeparator === -1) {
    return PACKAGE_NAME_RE.test(key) ? { name: key, versions: null } : null;
  }

  const name = key.slice(0, specSeparator);
  const rawVersionSpec = key.slice(specSeparator + 1).trim();
  if (!PACKAGE_NAME_RE.test(name) || !rawVersionSpec) {
    return null;
  }

  if (rawVersionSpec === "*") {
    return { name, versions: null };
  }

  const parts = rawVersionSpec.split("||").map((part) => part.trim());
  if (parts.length === 0 || parts.some((part) => !EXACT_SEMVER_RE.test(part))) {
    return null;
  }

  return { name, versions: new Set(parts) };
}

function validateAllowScriptsPolicy(allowScripts: unknown): string[] {
  if (!allowScripts || typeof allowScripts !== "object" || Array.isArray(allowScripts)) {
    return ["package.json allowScripts must be a plain object mapping package specs to booleans"];
  }

  const errors: string[] = [];
  for (const [key, value] of Object.entries(allowScripts as Record<string, unknown>)) {
    if (typeof value !== "boolean") {
      errors.push(`allowScripts["${key}"] must be a boolean, got ${typeof value}`);
    }
    if (!parseAllowScriptsKey(key)) {
      errors.push(
        `allowScripts["${key}"] must be a bare package name or exact version pin(s) per npm RFC 868`,
      );
    }
  }
  return errors;
}

function matchesPlatformList(list: string[] | undefined, target: string): boolean {
  if (!Array.isArray(list) || list.length === 0) return true;
  if (list.includes(`!${target}`)) return false;
  const positives = list.filter((entry) => !entry.startsWith("!"));
  return positives.length === 0 || positives.includes(target) || positives.includes("any");
}

function matchesCurrentLinuxPlatform(entry: LockfilePackageEntry): boolean {
  return (
    matchesPlatformList(entry.os, "linux") &&
    matchesPlatformList(entry.cpu, "x64") &&
    matchesPlatformList(entry.libc, "glibc")
  );
}

function parseTrustedRegistryIdentity(
  resolved: string | undefined,
): { name: string; version: string } | null {
  if (typeof resolved !== "string" || !/^https?:\/\//.test(resolved)) return null;
  let pathname: string;
  try {
    pathname = decodeURIComponent(new URL(resolved).pathname);
  } catch {
    return null;
  }
  const sepIndex = pathname.indexOf("/-/");
  if (sepIndex <= 1) return null;

  const prefixSegments = pathname.slice(1, sepIndex).split("/").filter(Boolean);
  if (prefixSegments.length === 0) return null;

  const shortName = prefixSegments[prefixSegments.length - 1];
  const maybeScope = prefixSegments.length >= 2 ? prefixSegments[prefixSegments.length - 2] : null;
  const name = maybeScope?.startsWith("@") ? `${maybeScope}/${shortName}` : shortName;

  const tarball = pathname.slice(sepIndex + 3);
  if (!tarball.endsWith(".tgz") || tarball.includes("/")) return null;
  const stem = tarball.slice(0, -4);
  const versionPrefix = `${shortName}-`;
  if (!stem.startsWith(versionPrefix)) return null;

  const version = stem.slice(versionPrefix.length);
  return version ? { name, version } : null;
}

function isRegistryTarball(resolved: string | undefined): boolean {
  return parseTrustedRegistryIdentity(resolved) !== null;
}

function packageNameFromLockPath(pkgPath: string): string {
  const marker = "node_modules/";
  const index = pkgPath.lastIndexOf(marker);
  return index === -1 ? pkgPath : pkgPath.slice(index + marker.length);
}

function hasInstallLifecycleScript(entry: LockfilePackageEntry): boolean {
  if (entry.hasInstallScript === true) return true;
  const scripts = entry.scripts;
  if (!scripts) return false;
  if (scripts.preinstall || scripts.install || scripts.postinstall) return true;
  if (scripts.prepare && entry.resolved && !isRegistryTarball(entry.resolved)) return true;
  return false;
}

function resolveAllowScriptsVerdict(
  entry: LockfilePackageEntry,
  pkgPath: string,
  allowScripts: Record<string, boolean>,
): boolean | null {
  const trusted = parseTrustedRegistryIdentity(entry.resolved);
  // Only registry packages with verifiable tarball identity (or omitted resolved URL) can match registry specs.
  if (entry.resolved && !trusted) {
    return null;
  }

  const name = trusted?.name ?? entry.name ?? packageNameFromLockPath(pkgPath);
  const version = trusted?.version ?? (entry.resolved ? null : (entry.version ?? null));

  let anyAllow = false;
  let anyDeny = false;

  for (const [key, decision] of Object.entries(allowScripts)) {
    const parsed = parseAllowScriptsKey(key);
    if (!parsed || parsed.name !== name) continue;

    if (parsed.versions === null) {
      if (decision === false) anyDeny = true;
      else if (decision === true) anyAllow = true;
      continue;
    }

    if (!version) {
      if (decision === false) anyDeny = true;
      continue;
    }

    if (parsed.versions.has(version)) {
      if (decision === false) anyDeny = true;
      else if (decision === true) anyAllow = true;
    }
  }

  if (anyDeny) return false;
  if (anyAllow) return true;
  return null;
}

function findUnreviewedLinuxInstallScripts(
  lockfile: PackageLockfile,
  allowScripts: Record<string, boolean>,
): string[] {
  const unreviewed: string[] = [];

  for (const [pkgPath, entry] of Object.entries(lockfile.packages ?? {})) {
    if (pkgPath === "" || entry.link || entry.inBundle) continue;
    if (entry.optional && !matchesCurrentLinuxPlatform(entry)) continue;
    if (!hasInstallLifecycleScript(entry)) continue;

    const verdict = resolveAllowScriptsVerdict(entry, pkgPath, allowScripts);
    if (verdict === true || verdict === false) continue;

    const trusted = parseTrustedRegistryIdentity(entry.resolved);
    const name = trusted?.name ?? entry.name ?? packageNameFromLockPath(pkgPath);
    const version = trusted?.version ?? entry.version ?? "unknown";
    unreviewed.push(`${pkgPath} (${name}@${version})`);
  }

  return unreviewed;
}

function findStaleAllowScriptsEntries(
  lockfile: PackageLockfile,
  allowScripts: Record<string, boolean>,
): string[] {
  const stale: string[] = [];
  const entries = Object.entries(lockfile.packages ?? {}).filter(
    ([pkgPath, entry]) => pkgPath !== "" && !entry.link && !entry.inBundle,
  );

  for (const key of Object.keys(allowScripts)) {
    const parsed = parseAllowScriptsKey(key);
    if (!parsed) continue;
    const matched = entries.some(([pkgPath, entry]) => {
      const trusted = parseTrustedRegistryIdentity(entry.resolved);
      const name = trusted?.name ?? entry.name ?? packageNameFromLockPath(pkgPath);
      const version = trusted?.version ?? entry.version ?? null;
      if (name !== parsed.name) return false;
      return parsed.versions === null || (version !== null && parsed.versions.has(version));
    });
    if (!matched) {
      stale.push(key);
    }
  }

  return stale;
}

function parseDockerfileStages(source: string): DockerfileStage[] {
  const normalized = source.replace(/\\\r?\n/g, " ");
  const stages: DockerfileStage[] = [];
  let current: DockerfileStage | null = null;

  for (const rawLine of normalized.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const fromMatch = /^FROM\s+\S+(?:\s+AS\s+([A-Za-z0-9._-]+))?/i.exec(line);
    if (fromMatch) {
      current = { name: fromMatch[1] ?? `stage-${stages.length}`, lines: [] };
      stages.push(current);
      continue;
    }
    current?.lines.push(line);
  }

  return stages;
}

function extractNpmInstallCommands(source: string): string[] {
  const commands: string[] = [];
  const segments = source
    .replace(/\\\r?\n/g, " ")
    .split(/\r?\n|&&|\|\||;/)
    .map((segment) => segment.trim())
    .filter((segment) => segment && !segment.startsWith("#"));

  for (const segment of segments) {
    const cleaned = segment
      .replace(/^(?:-\s+)?run:\s*(?:[|>][-+]?)?\s*/i, "")
      .replace(/^RUN\s+/i, "")
      .trim();
    if (/^npm\s+(?:ci|install|i)\b/.test(cleaned)) {
      commands.push(cleaned);
    }
  }

  return commands;
}

function hasStrictIgnoreScriptsFlag(command: string): boolean {
  const tokens = command.split(/\s+/);
  const hasIgnoreScripts = tokens.includes("--ignore-scripts") || tokens.includes("--ignore-scripts=true");
  const disablesIgnoreScripts = tokens.some(
    (token) =>
      token === "--ignore-scripts=false" ||
      token === "--no-ignore-scripts" ||
      token === "--foreground-scripts" ||
      token.startsWith("--allow-scripts"),
  );
  return hasIgnoreScripts && !disablesIgnoreScripts;
}

function parseWorkflowJobs(workflowSource: string): Map<string, string> {
  const lines = workflowSource.split(/\r?\n/);
  const jobs = new Map<string, string>();
  let inJobs = false;
  let currentJob: string | null = null;
  let currentLines: string[] = [];

  for (const line of lines) {
    if (!inJobs) {
      if (/^jobs:\s*$/.test(line)) inJobs = true;
      continue;
    }
    if (/^\S/.test(line) && !line.startsWith("#")) {
      break;
    }
    const jobMatch = /^  ([A-Za-z0-9_-]+):\s*$/.exec(line);
    if (jobMatch) {
      if (currentJob) jobs.set(currentJob, currentLines.join("\n"));
      currentJob = jobMatch[1];
      currentLines = [];
      continue;
    }
    if (currentJob) currentLines.push(line);
  }

  if (currentJob) {
    jobs.set(currentJob, currentLines.join("\n"));
  }

  return jobs;
}

describe("npm install-script policy guardrails", () => {
  it("enforces engine-strict, ignore-scripts, and strict-allow-scripts in .npmrc", () => {
    const npmrc = parseNpmrc(readRepoFile("../../.npmrc"));
    expect(npmrc.get("engine-strict")).toBe("true");
    expect(npmrc.get("ignore-scripts")).toBe("true");
    expect(npmrc.get("strict-allow-scripts")).toBe("true");

    const dockerignore = readRepoFile("../../.dockerignore")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"));
    expect(dockerignore).not.toContain(".npmrc");
    expect(dockerignore).not.toContain(".*");
  });

  it("enforces an RFC 868 boolean allowScripts map in package.json with no unreviewed Linux install scripts", () => {
    const pkg = JSON.parse(readRepoFile("../../package.json")) as { allowScripts?: unknown };
    const lockfile = JSON.parse(readRepoFile("../../package-lock.json")) as PackageLockfile;

    expect(validateAllowScriptsPolicy(pkg.allowScripts)).toEqual([]);

    const allowScripts = pkg.allowScripts as Record<string, boolean>;
    expect(findStaleAllowScriptsEntries(lockfile, allowScripts)).toEqual([]);
    expect(findUnreviewedLinuxInstallScripts(lockfile, allowScripts)).toEqual([]);
  });

  it("copies .npmrc and passes --ignore-scripts in every Dockerfile npm ci stage", () => {
    const stages = parseDockerfileStages(readRepoFile("../../Dockerfile"));
    const stageNames = stages.map((stage) => stage.name);
    expect(stageNames).toContain("build");
    expect(stageNames).toContain("runtime");

    for (const stage of stages) {
      let copiedNpmrc = false;
      const stageInstallCommands: string[] = [];

      for (const line of stage.lines) {
        if (/^COPY\b/i.test(line) && /(?:^|\s|\/)\.npmrc(?:\s|$)/.test(line)) {
          copiedNpmrc = true;
        }
        const commands = extractNpmInstallCommands(line);
        if (commands.length > 0) {
          expect(
            copiedNpmrc,
            `Dockerfile stage "${stage.name}" runs npm install/ci before copying .npmrc`,
          ).toBe(true);
          stageInstallCommands.push(...commands);
        }
      }

      expect(
        stageInstallCommands.length,
        `Dockerfile stage "${stage.name}" should run npm ci`,
      ).toBeGreaterThan(0);
      for (const command of stageInstallCommands) {
        expect(
          hasStrictIgnoreScriptsFlag(command),
          `Dockerfile stage "${stage.name}" command "${command}" must pass --ignore-scripts`,
        ).toBe(true);
      }
    }
  });

  it("passes --ignore-scripts for every npm install command in CI workflows", () => {
    const ciSource = readRepoFile("../../.github/workflows/ci.yml");
    const jobs = parseWorkflowJobs(ciSource);

    for (const requiredJob of ["web", "smoke"]) {
      const jobBody = jobs.get(requiredJob);
      expect(jobBody, `ci.yml should define the "${requiredJob}" job`).toBeDefined();
      const commands = extractNpmInstallCommands(jobBody ?? "");
      expect(
        commands.length,
        `ci.yml job "${requiredJob}" should run npm ci`,
      ).toBeGreaterThan(0);
      for (const command of commands) {
        expect(
          hasStrictIgnoreScriptsFlag(command),
          `ci.yml job "${requiredJob}" command "${command}" must pass --ignore-scripts`,
        ).toBe(true);
      }
    }

    const allCommands = extractNpmInstallCommands(ciSource);
    expect(allCommands.every(hasStrictIgnoreScriptsFlag)).toBe(true);
  });

  it("rejects malformed allowScripts policies, regressed flags, and unreviewed Linux scripts", () => {
    expect(validateAllowScriptsPolicy(["esbuild"])).toEqual([
      "package.json allowScripts must be a plain object mapping package specs to booleans",
    ]);
    expect(
      validateAllowScriptsPolicy({
        esbuild: "true",
        "bad-range@^1.2.0": false,
        "bad-tag@latest": true,
      }),
    ).toEqual([
      'allowScripts["esbuild"] must be a boolean, got string',
      'allowScripts["bad-range@^1.2.0"] must be a bare package name or exact version pin(s) per npm RFC 868',
      'allowScripts["bad-tag@latest"] must be a bare package name or exact version pin(s) per npm RFC 868',
    ]);

    const fixtureLockfile: PackageLockfile = {
      packages: {
        "": { name: "bookward", version: "0.0.1", scripts: { prepare: "svelte-kit sync" } },
        "node_modules/fsevents": {
          version: "2.3.3",
          resolved: "https://registry.npmjs.org/fsevents/-/fsevents-2.3.3.tgz",
          os: ["darwin"],
          optional: true,
          hasInstallScript: true,
        },
        "node_modules/arm64-native-addon": {
          version: "1.4.2",
          resolved: "https://registry.npmjs.org/arm64-native-addon/-/arm64-native-addon-1.4.2.tgz",
          optional: true,
          os: ["linux"],
          cpu: ["arm64"],
          hasInstallScript: true,
        },
        "node_modules/musl-native-addon": {
          version: "1.4.2",
          resolved: "https://registry.npmjs.org/musl-native-addon/-/musl-native-addon-1.4.2.tgz",
          optional: true,
          os: ["linux"],
          cpu: ["x64"],
          libc: ["musl"],
          hasInstallScript: true,
        },
        "node_modules/@scope/native-addon": {
          version: "1.4.2",
          resolved: "https://registry.npmjs.org/@scope/native-addon/-/native-addon-1.4.2.tgz",
          hasInstallScript: true,
        },
      },
    };

    expect(findUnreviewedLinuxInstallScripts(fixtureLockfile, {})).toEqual([
      "node_modules/@scope/native-addon (@scope/native-addon@1.4.2)",
    ]);
    expect(
      findUnreviewedLinuxInstallScripts(fixtureLockfile, { "@scope/native-addon@1.4.1": false }),
    ).toEqual(["node_modules/@scope/native-addon (@scope/native-addon@1.4.2)"]);
    expect(
      findUnreviewedLinuxInstallScripts(fixtureLockfile, { "@scope/native-addon@1.4.2": false }),
    ).toEqual([]);
    expect(
      findUnreviewedLinuxInstallScripts(fixtureLockfile, { "@scope/native-addon": true }),
    ).toEqual([]);
    expect(findStaleAllowScriptsEntries(fixtureLockfile, { esbuild: false })).toEqual([
      "esbuild",
    ]);

    expect(hasStrictIgnoreScriptsFlag("npm ci")).toBe(false);
    expect(hasStrictIgnoreScriptsFlag("npm ci --ignore-scripts")).toBe(true);
    expect(hasStrictIgnoreScriptsFlag("npm ci --omit=dev --ignore-scripts")).toBe(true);
    expect(hasStrictIgnoreScriptsFlag("npm ci --ignore-scripts --ignore-scripts=false")).toBe(
      false,
    );
  });
});
