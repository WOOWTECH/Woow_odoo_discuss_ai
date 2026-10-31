/** @odoo-module */
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

// "/ai" (status), "/ai on" (resume the AI now), "/ai off" (pause it) in Live Chat conversations.
registry.category("discuss.channel_commands").add("ai", {
    channel_types: ["livechat"],
    help: _t("AI contact: /ai on resumes it now, /ai off pauses it"),
    methodName: "execute_command_woow_ai",
});
