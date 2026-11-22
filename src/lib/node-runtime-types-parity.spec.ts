import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function readRepoFile(pathFromRepoRoot: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../../${pathFromRepoRoot}`, import.meta.url)),
    "utf8",
  );
}

function parseMajor(versionSpec: string | undefined): number {
  const match = versionSpec?.trim().match(/^[~^]?(\d+)\.\d+\.\d+/);
  return match ? Number(match[1]) : Number.NaN;
}

function parseYamlScalar(rawValue: string): string {
  const trimmed = rawValue.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  if (trimmed.startsWith("@")) {
    throw new Error(`Unquoted leading '@' is invalid YAML scalar syntax: ${trimmed}`);
  }
  return trimmed;
}

function findNpmUpdateBlock(dependabotSource: string): string | undefined {
  const ecosystems = dependabotSource.split(/\n(?=\s{2}-\s+package-ecosystem:)/);
  return ecosystems.find(
    (block) =>
      /^\s{2}-\s+package-ecosystem:\s*["']?npm["']?\s*$/m.test(block) &&
      /^\s{4}directory:\s*["']?\/["']?\s*$/m.test(block),
  );
}

function parseIgnoreRules(ecosystemBlock: string): Map<string, string[]> {
  const rules = new Map<string, string[]>();
  const lines = ecosystemBlock.split(/\r?\n/);
  let inIgnore = false;
  let currentDependency: string | null = null;
  let inVersions = false;

  for (const line of lines) {
    if (/^\s*(?:#.*)?$/.test(line)) continue;

    if (!inIgnore) {
      if (/^ {4}ignore:\s*$/.test(line)) {
        inIgnore = true;
      }
      continue;
    }

    if (/^ {0,4}\S/.test(line)) {
      break;
    }

    const depMatch = line.match(/^ {6}-\s+dependency-name:\s*(.+)$/);
    if (depMatch) {
      currentDependency = parseYamlScalar(depMatch[1]);
      rules.set(currentDependency, []);
      inVersions = false;
      continue;
    }

    if (/^ {8}versions:\s*$/.test(line)) {
      inVersions = true;
      continue;
    }

    if (/^ {6,8}\S/.test(line)) {
      inVersions = false;
      continue;
    }

    if (inVersions && currentDependency) {
      const versionMatch = line.match(/^ {10}-\s+(.+)$/);
      if (versionMatch) {
        rules.get(currentDependency)?.push(parseYamlScalar(versionMatch[1]));
      }
    }
  }

  return rules;
}

describe("Node runtime and @types/node parity", () => {
  it("keeps @types/node and Dependabot aligned with the Node 24 container and CI runtime", () => {
    const dockerfile = readRepoFile("Dockerfile");
    const dockerfileNodeMajors = [...dockerfile.matchAll(/^FROM\s+node:(\d+)\b/gm)].map((match) =>
      Number(match[1]),
    );
    expect(dockerfileNodeMajors).toEqual([24, 24]);
    const runtimeNodeMajor = dockerfileNodeMajors[0];

    const ciWorkflow = readRepoFile(".github/workflows/ci.yml");
    const ciNodeMajors = [
      ...ciWorkflow.matchAll(/^\s*node-version:\s*["']?(\d+)(?:\.\d+)*["']?\s*$/gm),
    ].map((match) => Number(match[1]));
    expect(ciNodeMajors.length).toBeGreaterThanOrEqual(2);
    expect(new Set(ciNodeMajors)).toEqual(new Set([runtimeNodeMajor]));

    const packageJson = JSON.parse(readRepoFile("package.json")) as {
      devDependencies?: Record<string, string>;
    };
    const packageLock = JSON.parse(readRepoFile("package-lock.json")) as {
      packages?: Record<
        string,
        { version?: string; devDependencies?: Record<string, string> }
      >;
    };

    const declaredTypesNode = packageJson.devDependencies?.["@types/node"];
    const rootLockedTypesNode = packageLock.packages?.[""]?.devDependencies?.["@types/node"];
    const resolvedTypesNode = packageLock.packages?.["node_modules/@types/node"]?.version;

    expect(declaredTypesNode).toBeDefined();
    expect(rootLockedTypesNode).toBe(declaredTypesNode);
    expect(parseMajor(declaredTypesNode)).toBe(runtimeNodeMajor);
    expect(parseMajor(resolvedTypesNode)).toBe(runtimeNodeMajor);

    const dependabotConfig = readRepoFile(".github/dependabot.yml");
    const npmUpdateBlock = findNpmUpdateBlock(dependabotConfig);
    expect(npmUpdateBlock, ".github/dependabot.yml should configure root npm updates").toBeDefined();
    expect(npmUpdateBlock).toMatch(/^\s{6}interval:\s*["']?weekly["']?\s*$/m);
    expect(npmUpdateBlock).toMatch(
      /^\s{6}frontend-tooling:\s*\n\s{8}dependency-type:\s*["']?development["']?\s*$/m,
    );

    const ignoreRules = parseIgnoreRules(npmUpdateBlock!);
    expect(ignoreRules.get("@types/node")).toEqual([`>=${runtimeNodeMajor + 1}.0.0`]);
  });
});
