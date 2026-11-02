/** @odoo-module */
// No "Start a Call" / "Start a Video Call" in a chat with an AI contact: the
// bot never answers a call. Odoo does the same for OdooBot (Thread.allowCalls);
// AI contact partners have the "bot" presence (res.partner._compute_im_status).
import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    get allowCalls() {
        return (
            super.allowCalls &&
            !(this.channel_type === "chat" && this.correspondent?.persona?.im_status === "bot")
        );
    },
});
