import { ChildProcess, spawn } from 'child_process';
import { StataOutputChannel } from './terminal';

/**
 * JSON-RPC 2.0 request structure.
 */
interface JsonRpcRequest {
    jsonrpc: '2.0';
    id: number;
    method: string;
    params?: Record<string, unknown>;
}

/**
 * JSON-RPC 2.0 response structure.
 */
interface JsonRpcResponse {
    jsonrpc: '2.0';
    id: number;
    result?: unknown;
    error?: {
        code: number;
        message: string;
        data?: unknown;
    };
}

/**
 * Result returned from MCP tool calls.
 */
export interface McpToolResult {
    output?: string;
    error?: string;
    graph?: GraphData;
}

/**
 * Graph data returned from Stata.
 */
export interface GraphData {
    format: string;
    base64: string;
    filename?: string;
}

/**
 * Pending request tracking entry.
 */
interface PendingRequest {
    resolve: (value: JsonRpcResponse) => void;
    reject: (reason: Error) => void;
    timer: ReturnType<typeof setTimeout>;
}

/**
 * MCP client bridge that communicates with the stata-ai-fusion MCP server
 * via JSON-RPC 2.0 over stdin/stdout.
 */
export class McpBridge {
    private process: ChildProcess | null = null;
    private nextId = 1;
    private pendingRequests: Map<number, PendingRequest> = new Map();
    private buffer = '';
    private outputChannel: StataOutputChannel;
    private started = false;
    private timeout: number;

    constructor(outputChannel: StataOutputChannel) {
        this.outputChannel = outputChannel;
        this.timeout = 120000; // default timeout
    }

    /**
     * Start the MCP server process if not already running.
     */
    async start(): Promise<void> {
        if (this.started && this.process && !this.process.killed) {
            return;
        }

        return new Promise<void>((resolve, reject) => {
            try {
                this.outputChannel.appendLine(
                    '[MCP Bridge] Starting MCP server via uvx...'
                );

                this.process = spawn(
                    'uvx',
                    ['--from', 'stata-ai-fusion', 'stata-ai-fusion'],
                    {
                        stdio: ['pipe', 'pipe', 'pipe'],
                        env: { ...process.env },
                    }
                );

                this.buffer = '';

                this.process.stdout?.on('data', (data: Buffer) => {
                    this.handleStdout(data.toString('utf-8'));
                });

                this.process.stderr?.on('data', (data: Buffer) => {
                    const text = data.toString('utf-8');
                    this.outputChannel.appendLine(`[MCP Server] ${text.trim()}`);
                });

                this.process.on('error', (err: Error) => {
                    this.outputChannel.appendLine(
                        `[MCP Bridge] Process error: ${err.message}`
                    );
                    this.started = false;
                    this.rejectAllPending(
                        new Error(`MCP server process error: ${err.message}`)
                    );
                    reject(err);
                });

                this.process.on('exit', (code: number | null) => {
                    this.outputChannel.appendLine(
                        `[MCP Bridge] Process exited with code ${code}`
                    );
                    this.started = false;
                    this.rejectAllPending(
                        new Error(`MCP server exited with code ${code}`)
                    );
                });

                // Send initialize request per MCP protocol
                const initResult = this.callToolRaw('initialize', {
                    protocolVersion: '2024-11-05',
                    capabilities: {},
                    clientInfo: {
                        name: 'stata-ai-fusion-vscode',
                        version: '0.2.2',
                    },
                });

                initResult
                    .then(() => {
                        this.started = true;
                        this.outputChannel.appendLine(
                            '[MCP Bridge] MCP server initialized.'
                        );
                        resolve();
                    })
                    .catch((err: Error) => {
                        this.outputChannel.appendLine(
                            `[MCP Bridge] Init failed: ${err.message}`
                        );
                        reject(err);
                    });
            } catch (err) {
                const message =
                    err instanceof Error ? err.message : String(err);
                this.outputChannel.appendLine(
                    `[MCP Bridge] Failed to start: ${message}`
                );
                reject(err);
            }
        });
    }

    /**
     * Send a raw JSON-RPC request and return the response.
     */
    private callToolRaw(
        method: string,
        params?: Record<string, unknown>
    ): Promise<JsonRpcResponse> {
        return new Promise<JsonRpcResponse>((resolve, reject) => {
            if (!this.process?.stdin?.writable) {
                reject(new Error('MCP server process is not running.'));
                return;
            }

            const id = this.nextId++;
            const request: JsonRpcRequest = {
                jsonrpc: '2.0',
                id,
                method,
                params,
            };

            const timer = setTimeout(() => {
                const pending = this.pendingRequests.get(id);
                if (pending) {
                    this.pendingRequests.delete(id);
                    pending.reject(
                        new Error(
                            `Request ${id} timed out after ${this.timeout}ms`
                        )
                    );
                }
            }, this.timeout);

            this.pendingRequests.set(id, { resolve, reject, timer });

            const message = JSON.stringify(request) + '\n';
            this.process.stdin.write(message, (err) => {
                if (err) {
                    this.pendingRequests.delete(id);
                    clearTimeout(timer);
                    reject(
                        new Error(
                            `Failed to write to MCP server: ${err.message}`
                        )
                    );
                }
            });
        });
    }

    /**
     * Handle incoming stdout data, buffering and parsing JSON-RPC responses.
     */
    private handleStdout(data: string): void {
        this.buffer += data;

        // Process complete lines (newline-delimited JSON)
        const lines = this.buffer.split('\n');
        // Keep the last incomplete line in the buffer
        this.buffer = lines.pop() ?? '';

        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) {
                continue;
            }

            try {
                const response = JSON.parse(trimmed) as JsonRpcResponse;

                if (response.id !== undefined) {
                    const pending = this.pendingRequests.get(response.id);
                    if (pending) {
                        this.pendingRequests.delete(response.id);
                        clearTimeout(pending.timer);
                        pending.resolve(response);
                    }
                }
            } catch {
                // Not valid JSON; might be a notification or log line
                this.outputChannel.appendLine(
                    `[MCP Bridge] Non-JSON output: ${trimmed}`
                );
            }
        }
    }

    /**
     * Call an MCP tool by name with arguments.
     */
    async callTool(
        name: string,
        args: Record<string, unknown>
    ): Promise<McpToolResult> {
        const response = await this.callToolRaw('tools/call', {
            name,
            arguments: args,
        });

        if (response.error) {
            return {
                error: response.error.message,
            };
        }

        const result = response.result as Record<string, unknown> | undefined;
        if (!result) {
            return { output: '' };
        }

        // Parse MCP tool result content array
        const content = result.content as Array<Record<string, unknown>> | undefined;
        if (Array.isArray(content)) {
            let output = '';
            let graph: GraphData | undefined;

            for (const item of content) {
                if (item.type === 'text') {
                    output += (item.text as string) + '\n';
                } else if (item.type === 'image') {
                    graph = {
                        format: (item.mimeType as string)?.split('/')[1] ?? 'png',
                        base64: item.data as string,
                    };
                }
            }

            return { output: output.trimEnd(), graph };
        }

        return { output: String(result) };
    }

    /**
     * Run a Stata command string.
     */
    async runCommand(code: string): Promise<McpToolResult> {
        return this.callTool('run_command', { code });
    }

    /**
     * Run a Stata .do file by path.
     */
    async runDoFile(filePath: string): Promise<McpToolResult> {
        return this.callTool('run_do_file', { file_path: filePath });
    }

    /**
     * Stop the MCP server process and clean up pending requests.
     */
    stop(): void {
        this.rejectAllPending(new Error('MCP bridge stopped by user.'));

        if (this.process) {
            try {
                this.process.stdin?.end();
                this.process.kill('SIGTERM');

                // Force kill after 5 seconds if still running
                setTimeout(() => {
                    if (this.process && !this.process.killed) {
                        this.process.kill('SIGKILL');
                    }
                }, 5000);
            } catch {
                // Process may have already exited
            }
            this.process = null;
        }

        this.started = false;
        this.buffer = '';
    }

    /**
     * Reject all pending requests with the given error.
     */
    private rejectAllPending(error: Error): void {
        for (const [id, pending] of this.pendingRequests) {
            clearTimeout(pending.timer);
            pending.reject(error);
            this.pendingRequests.delete(id);
        }
    }

    /**
     * Update the request timeout value.
     */
    setTimeout(ms: number): void {
        this.timeout = ms;
    }

    /**
     * Check whether the MCP server is currently running.
     */
    get isRunning(): boolean {
        return this.started && this.process !== null && !this.process.killed;
    }
}
