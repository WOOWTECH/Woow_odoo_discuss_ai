/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";

import { discussSidebarItemsRegistry } from "@mail/core/public_web/discuss_sidebar";

import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";

/**
 * "AI Contacts" section of the Discuss sidebar (backend only), above the
 * channel and direct message categories. Lists the AI contacts the current
 * user may chat with; a click finds or creates the direct chat and opens it.
 */
export class WoowAiContactsSidebar extends Component {
    static template = "woow_ai_contact.DiscussSidebarAiContacts";
    static props = {};

    setup() {
        super.setup();
        this.store = useState(useService("mail.store"));
        this.notification = useService("notification");
        this.state = useState({ contacts: [], open: true, opening: false });
        onWillStart(() => this.loadContacts());
    }

    async loadContacts() {
        if (this.store.inPublicPage) {
            return;
        }
        try {
            this.state.contacts = await rpc("/woow_ai/contacts", {});
        } catch {
            this.state.contacts = [];
        }
    }

    get title() {
        return _t("AI Contacts");
    }

    avatarUrl(contact) {
        return `/web/image/res.partner/${contact.partner_id}/avatar_128`;
    }

    isActive(contact) {
        const thread = this.store.discuss.thread;
        return Boolean(
            contact.channel_id &&
                thread &&
                thread.model === "discuss.channel" &&
                thread.id === contact.channel_id
        );
    }

    async openContact(contact) {
        if (this.state.opening) {
            return;
        }
        this.state.opening = true;
        try {
            const result = await rpc("/woow_ai/open", { contact_id: contact.id });
            this.store.insert(result.store_data, { html: true });
            contact.channel_id = result.channel_id;
            const thread = this.store.Thread.get({ model: "discuss.channel", id: result.channel_id });
            if (thread) {
                thread.open();
            }
        } catch {
            this.notification.add(_t("Could not open the conversation with this AI contact."), {
                type: "danger",
            });
        } finally {
            this.state.opening = false;
        }
    }
}

discussSidebarItemsRegistry.add("woow_ai_contacts", WoowAiContactsSidebar, { sequence: 25 });
