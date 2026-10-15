import { execFileSync } from "node:child_process";
import { dirname } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.on("session_start", (_event, ctx) => {
    const pane = process.env.TMUX_PANE;
    const file = ctx.sessionManager.getSessionFile();
    if (!pane || !file) return;
    const tmux = (...args: string[]) => execFileSync("tmux", args, { encoding: "utf8" }).trim();
    if (!tmux("display-message", "-p", "-t", pane, "#{@pit_status}")) return;
    tmux("set-option", "-p", "-t", pane, "@pit_session", JSON.stringify({
      session_id: ctx.sessionManager.getSessionId(), session_dir: dirname(file),
    }));
  });
}
