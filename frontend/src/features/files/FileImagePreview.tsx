import { RefreshCw, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useState } from "react";

import { Button, DialogBody, InlineAlert, Skeleton, cn } from "../../shared/ui";
import { sessionFilePreviewUrl } from "./model";

export interface FileImagePreviewProps {
  displayName: string;
  fileId: string;
  sessionId: string;
}

/**
 * 白名单位图原图灯箱（PNG / JPEG / WEBP）：
 * - 管理加载态、失败重载计数与适应窗口 / 100% 原始尺寸缩放切换；
 * - 切换目标文件时自动重置缩放与加载状态。
 */
export function FileImagePreview({
  displayName,
  fileId,
  sessionId,
}: FileImagePreviewProps) {
  const [imageZoomActual, setImageZoomActual] = useState(false);
  const [imageLoaded, setImageLoaded] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [imageReloadCount, setImageReloadCount] = useState(0);

  // 切换预览文件时重置灯箱缩放、加载进度与重试次数。
  useEffect(() => {
    setImageZoomActual(false);
    setImageLoaded(false);
    setImageFailed(false);
    setImageReloadCount(0);
  }, [fileId, sessionId]);

  const imagePreviewUrl = `${sessionFilePreviewUrl(sessionId, fileId)}${
    imageReloadCount > 0 ? `?retry=${imageReloadCount}` : ""
  }`;

  return (
    <>
      {!imageFailed ? (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-subtle/60 px-6 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              onClick={() => setImageZoomActual((current) => !current)}
              size="xs"
              variant="outline"
            >
              {imageZoomActual ? (
                <>
                  <ZoomOut aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>适应窗口</span>
                </>
              ) : (
                <>
                  <ZoomIn aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>100% 原始尺寸</span>
                </>
              )}
            </Button>
            <span className="text-xs text-text-tertiary">
              点击图片可切换缩放比例
            </span>
          </div>
        </div>
      ) : null}

      <DialogBody className="space-y-4">
        {imageFailed ? (
          <InlineAlert
            action={
              <Button
                onClick={() => {
                  setImageFailed(false);
                  setImageLoaded(false);
                  setImageReloadCount((count) => count + 1);
                }}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重新加载
              </Button>
            }
            description="图片可能已过期或文件服务暂时不可用，请重试或点击下方按钮下载原件。"
            title="原图预览加载失败"
            variant="warning"
          />
        ) : (
          <div
            className={cn(
              "relative flex min-h-[260px] w-full overflow-auto rounded-xl border border-border-subtle bg-subtle p-3",
              imageZoomActual
                ? "max-h-[64vh] items-start justify-start"
                : "max-h-[64vh] items-center justify-center",
            )}
            data-image-zoom={imageZoomActual ? "actual" : "fit"}
          >
            {!imageLoaded ? (
              <div
                aria-label="正在加载原图"
                className="pointer-events-none absolute inset-3 flex items-center justify-center"
                role="status"
              >
                <Skeleton className="h-48 w-full max-w-md rounded-lg" />
              </div>
            ) : null}
            <button
              aria-label={
                imageZoomActual
                  ? `缩放至适应窗口：${displayName}`
                  : `切换至 100% 原始尺寸：${displayName}`
              }
              className={cn(
                "relative rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                imageZoomActual
                  ? "shrink-0 cursor-zoom-out"
                  : "min-w-0 max-w-full cursor-zoom-in",
              )}
              onClick={() => setImageZoomActual((current) => !current)}
              type="button"
            >
              <img
                alt={displayName}
                className={cn(
                  "block rounded-lg transition-opacity duration-150",
                  imageZoomActual
                    ? "h-auto max-w-none w-auto"
                    : "max-h-[58vh] max-w-full object-contain",
                )}
                onError={() => {
                  setImageFailed(true);
                  setImageLoaded(false);
                }}
                onLoad={() => {
                  setImageLoaded(true);
                  setImageFailed(false);
                }}
                src={imagePreviewUrl}
              />
            </button>
          </div>
        )}
      </DialogBody>
    </>
  );
}
