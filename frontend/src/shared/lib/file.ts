/**
 * 提取文件名末段扩展名（去除前导点号与首尾空白，并规范为小写）；
 * 无扩展名或点号在末尾时返回空字符串。
 */
export function fileExtension(filename: string): string {
  const index = filename.lastIndexOf(".");
  return index >= 0 ? filename.slice(index + 1).trim().toLowerCase() : "";
}
