import * as vscode from 'vscode';
import { McpBridge } from './mcpBridge';
import { StataOutputChannel } from './terminal';
import { GraphPreview } from './graphPreview';
import { autoConfigureMcp } from './mcpConfig';

let mcpBridge: McpBridge | undefined;
let outputChannel: StataOutputChannel | undefined;
let statusBarItem: vscode.StatusBarItem | undefined;

export function activate(context: vscode.ExtensionContext): void {
    outputChannel = new StataOutputChannel();
    mcpBridge = new McpBridge(outputChannel);

    const timeoutConfig = vscode.workspace.getConfiguration('stataFusion');
    const timeout = timeoutConfig.get<number>('requestTimeout', 120000);
    mcpBridge.setTimeout(timeout);

    statusBarItem = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Left,
        100
    );
    statusBarItem.text = '$(terminal) Stata AI Fusion';
    statusBarItem.tooltip = 'Stata AI Fusion - Ready';
    statusBarItem.show();

    // Register commands
    const runSelectionCmd = vscode.commands.registerCommand(
        'stata-fusion.runSelection',
        async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showWarningMessage('No active editor found.');
                return;
            }

            let code: string;
            if (editor.selection.isEmpty) {
                // Run current line
                const line = editor.document.lineAt(editor.selection.active.line);
                code = line.text.trim();
            } else {
                // Run selection
                code = editor.document.getText(editor.selection);
            }

            if (!code) {
                vscode.window.showWarningMessage('No code to run.');
                return;
            }

            await runStataCode(code);
        }
    );

    const runFileCmd = vscode.commands.registerCommand(
        'stata-fusion.runFile',
        async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showWarningMessage('No active editor found.');
                return;
            }

            const filePath = editor.document.uri.fsPath;
            if (!filePath) {
                vscode.window.showWarningMessage('Cannot determine file path.');
                return;
            }

            await runStataFile(filePath);
        }
    );

    const stopExecutionCmd = vscode.commands.registerCommand(
        'stata-fusion.stopExecution',
        async () => {
            if (!mcpBridge) {
                vscode.window.showWarningMessage('MCP bridge is not active.');
                return;
            }

            try {
                mcpBridge.stop();
                outputChannel?.appendLine('[Stata AI Fusion] Execution stopped.');
                vscode.window.showInformationMessage('Stata execution stopped.');
            } catch (err) {
                const message = err instanceof Error ? err.message : String(err);
                vscode.window.showErrorMessage(
                    `Failed to stop execution: ${message}`
                );
            }
        }
    );

    context.subscriptions.push(
        runSelectionCmd,
        runFileCmd,
        stopExecutionCmd,
        statusBarItem
    );

    // Auto-configure MCP if enabled
    const config = vscode.workspace.getConfiguration('stataFusion');
    if (config.get<boolean>('autoConfigureMcp', true)) {
        autoConfigureMcp().catch((err: unknown) => {
            const message = err instanceof Error ? err.message : String(err);
            outputChannel?.appendLine(
                `[Stata AI Fusion] MCP auto-config warning: ${message}`
            );
        });
    }

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('stataFusion.requestTimeout') && mcpBridge) {
                const updated = vscode.workspace.getConfiguration('stataFusion');
                mcpBridge.setTimeout(updated.get<number>('requestTimeout', 120000));
            }
        })
    );

    outputChannel.appendLine('[Stata AI Fusion] Extension activated.');
}

async function runStataCode(code: string): Promise<void> {
    if (!mcpBridge || !outputChannel) {
        return;
    }

    statusBarItem!.text = '$(sync~spin) Stata Running...';
    outputChannel.showRunStart(code);

    const startTime = Date.now();
    try {
        await mcpBridge.start();
        const result = await mcpBridge.runCommand(code);
        const elapsed = Date.now() - startTime;

        if (result.error) {
            outputChannel.showError(result.error, elapsed);
        } else {
            outputChannel.showResult(result.output ?? '', elapsed);
        }

        // Handle graph output
        if (result.graph) {
            GraphPreview.show(result.graph);
        }
    } catch (err) {
        const elapsed = Date.now() - startTime;
        const message = err instanceof Error ? err.message : String(err);
        outputChannel.showError(message, elapsed);
    } finally {
        statusBarItem!.text = '$(terminal) Stata AI Fusion';
    }
}

async function runStataFile(filePath: string): Promise<void> {
    if (!mcpBridge || !outputChannel) {
        return;
    }

    statusBarItem!.text = '$(sync~spin) Stata Running...';
    outputChannel.showRunStart(`do "${filePath}"`);

    const startTime = Date.now();
    try {
        await mcpBridge.start();
        const result = await mcpBridge.runDoFile(filePath);
        const elapsed = Date.now() - startTime;

        if (result.error) {
            outputChannel.showError(result.error, elapsed);
        } else {
            outputChannel.showResult(result.output ?? '', elapsed);
        }

        if (result.graph) {
            GraphPreview.show(result.graph);
        }
    } catch (err) {
        const elapsed = Date.now() - startTime;
        const message = err instanceof Error ? err.message : String(err);
        outputChannel.showError(message, elapsed);
    } finally {
        statusBarItem!.text = '$(terminal) Stata AI Fusion';
    }
}

export function deactivate(): void {
    if (mcpBridge) {
        mcpBridge.stop();
        mcpBridge = undefined;
    }
    if (outputChannel) {
        outputChannel.dispose();
        outputChannel = undefined;
    }
    if (statusBarItem) {
        statusBarItem.dispose();
        statusBarItem = undefined;
    }
}
