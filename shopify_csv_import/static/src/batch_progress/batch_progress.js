/** @odoo-module **/
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";

const POLL_MS = 3000;

/**
 * 导入批次的实时进度面板。
 *
 * 每 3 秒向服务器读一次进度（shopify.import.batch.get_progress_snapshot），批次在后台
 * 定时任务里跑，这个面板只负责"看"：关掉页面不影响后台，重新打开会接着显示最新进度。
 * 批次完成后停止轮询，并刷新一次表单（让按钮和状态栏也更新）。
 */
export class ShopifyBatchProgress extends Component {
    static template = "shopify_csv_import.BatchProgress";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ data: null, updatedAt: null, error: null });
        this.timer = null;
        // 浏览器会冻结后台标签页的定时器；切回这个页面时立刻刷新一次，不等下一轮
        this.onVisibilityChange = async () => {
            if (!document.hidden) {
                await this.refresh();
                this.schedule();
            }
        };
        onWillStart(() => this.refresh());
        onMounted(() => {
            document.addEventListener("visibilitychange", this.onVisibilityChange);
            this.schedule();
        });
        onWillUnmount(() => {
            clearTimeout(this.timer);
            document.removeEventListener("visibilitychange", this.onVisibilityChange);
        });
    }

    get resId() {
        return this.props.record.resId;
    }

    schedule() {
        clearTimeout(this.timer);
        if (this.state.data && !this.state.data.active) {
            return; // 已完成：不再轮询
        }
        this.timer = setTimeout(async () => {
            await this.refresh();
            this.schedule();
        }, POLL_MS);
    }

    async refresh() {
        if (!this.resId) {
            return;
        }
        try {
            const previous = this.state.data;
            const data = await this.orm.call("shopify.import.batch", "get_progress_snapshot", [this.resId]);
            this.state.data = data;
            this.state.error = null;
            this.state.updatedAt = new Date().toLocaleTimeString();
            if (previous && (previous.state !== data.state || previous.paused !== data.paused)) {
                await this.props.record.load();
            }
        } catch (e) {
            this.state.error = "进度读取失败，稍后自动重试";
        }
    }

    pct(value) {
        return `${Math.max(0, Math.min(100, value || 0)).toFixed(1)}%`;
    }
}

registry.category("view_widgets").add("shopify_batch_progress", {
    component: ShopifyBatchProgress,
});
