import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

/**
 * MCP server configuration entry structure.
 */
interface McpServerEntry {
    command: string;
    args: string[];
    env?: Record<string, string>;
}

/**
 * MCP config file structure.
 */
interface McpConfig {
    mcpServers?: Record<string, McpServerEntry>;
}

/**
 * IDE detection result.
 */
type IdeType = 'vscode' | 'cursor' | 'windsurf' | 'unknown';

/**
 * Detect which IDE is currently running based on the executable path
 * and environment variables.
 */
function detectIde(): IdeType {
    const execPath = process.execPath.toLowerCase();

    if (execPath.includes('cursor')) {
        return 'cursor';
    }
    if (execPath.includes('windsurf')) {
        return 'windsurf';
    }
    if (
        execPath.includes('code') ||
        execPath.includes('vscode')
    ) {
        return 'vscode';
    }

    // Fallback: check environment variables
    if (process.env.CURSOR_CHANNEL) {
        return 'cursor';
    }
    if (process.env.WINDSURF_CHANNEL) {
        return 'windsurf';
    }

    return 'vscode';
}

/**
 * Get the path to the MCP configuration file for the detected IDE.
 */
function getMcpConfigPath(ide: IdeType): string {
    const homeDir = os.homedir();
    const platform = process.platform;

    let configDir: string;

    if (platform === 'darwin') {
        // macOS
        const appSupport = path.join(
            homeDir,
            'Library',
            'Application Support'
        );
        switch (ide) {
            case 'cursor':
                configDir = path.join(appSupport, 'Cursor', 'User');
                break;
            case 'windsurf':
                configDir = path.join(appSupport, 'Windsurf', 'User');
                break;
            default:
                configDir = path.join(appSupport, 'Code', 'User');
                break;
        }
    } else if (platform === 'win32') {
        // Windows
        const appData = process.env.APPDATA || path.join(homeDir, 'AppData', 'Roaming');
        switch (ide) {
            case 'cursor':
                configDir = path.join(appData, 'Cursor', 'User');
                break;
            case 'windsurf':
                configDir = path.join(appData, 'Windsurf', 'User');
                break;
            default:
                configDir = path.join(appData, 'Code', 'User');
                break;
        }
    } else {
        // Linux
        const xdgConfig =
            process.env.XDG_CONFIG_HOME ||
            path.join(homeDir, '.config');
        switch (ide) {
            case 'cursor':
                configDir = path.join(xdgConfig, 'Cursor', 'User');
                break;
            case 'windsurf':
                configDir = path.join(xdgConfig, 'Windsurf', 'User');
                break;
            default:
                configDir = path.join(xdgConfig, 'Code', 'User');
                break;
        }
    }

    return path.join(configDir, 'mcp.json');
}

/**
 * Automatically configure the stata-ai-fusion MCP server entry in the
 * IDE's mcp.json configuration file. This function:
 *
 * 1. Detects the current IDE (VS Code, Cursor, Windsurf)
 * 2. Locates the appropriate mcp.json config path
 * 3. Reads existing config (if any)
 * 4. Adds the stata-ai-fusion server entry if not already present
 * 5. Writes the updated config back
 *
 * Does NOT overwrite existing server entries for stata-ai-fusion.
 */
export async function autoConfigureMcp(): Promise<void> {
    const ide = detectIde();
    const configPath = getMcpConfigPath(ide);
    const serverKey = 'stata-ai-fusion';

    const serverEntry: McpServerEntry = {
        command: 'uvx',
        args: ['--from', 'stata-ai-fusion', 'stata-ai-fusion'],
    };

    // Read existing config, or start fresh if the file does not exist.
    //
    // IMPORTANT: if the file exists but cannot be read/parsed (e.g. it contains
    // JSONC comments, which VS Code's mcp.json allows, or was hand-edited), we
    // must NOT fall back to an empty object and rewrite it — that would delete
    // every other MCP server the user has configured.  Skip auto-config
    // instead and leave the file untouched.
    let config: McpConfig = {};
    if (fs.existsSync(configPath)) {
        let content: string;
        try {
            content = fs.readFileSync(configPath, 'utf-8');
        } catch (err) {
            console.warn(
                `[stata-ai-fusion] Could not read ${configPath}; ` +
                    'skipping MCP auto-config.',
                err
            );
            return;
        }
        try {
            config = JSON.parse(content) as McpConfig;
        } catch (err) {
            console.warn(
                `[stata-ai-fusion] ${configPath} is not strict JSON ` +
                    '(comments?); skipping MCP auto-config to avoid overwriting ' +
                    'your other MCP servers. Add the "stata-ai-fusion" entry ' +
                    'manually if needed.',
                err
            );
            return;
        }
        // Guard against a structurally-unexpected file (null/array/primitive):
        // don't risk overwriting it.
        if (
            typeof config !== 'object' ||
            config === null ||
            Array.isArray(config)
        ) {
            console.warn(
                `[stata-ai-fusion] ${configPath} is not a JSON object; ` +
                    'skipping MCP auto-config to avoid overwriting it.'
            );
            return;
        }
    }

    // Initialize mcpServers if missing
    if (!config.mcpServers) {
        config.mcpServers = {};
    }

    // Do not overwrite existing config for this server
    if (config.mcpServers[serverKey]) {
        return;
    }

    // Add the server entry
    config.mcpServers[serverKey] = serverEntry;

    // Ensure the config directory exists
    const configDir = path.dirname(configPath);
    if (!fs.existsSync(configDir)) {
        fs.mkdirSync(configDir, { recursive: true });
    }

    // Write the config file
    fs.writeFileSync(
        configPath,
        JSON.stringify(config, null, 2) + '\n',
        'utf-8'
    );
}
