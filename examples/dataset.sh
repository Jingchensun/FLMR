huggingface-cli download BByrneLab/M2KR_Images --local-dir /scr/jingchen/data/M2KR_Images --repo-type dataset

# 进入目标目录
cd /scr/jingchen/data/M2KR_Images

# 递归查找并解压
find . -type f \( -name "*.zip" -o -name "*.tar" -o -name "*.tar.gz" \) | while read file; do
    echo "Extracting $file ..."
    dir=$(dirname "$file")
    if [[ "$file" == *.zip ]]; then
        unzip -o "$file" -d "$dir"
    elif [[ "$file" == *.tar ]]; then
        tar -xvf "$file" -C "$dir"
    elif [[ "$file" == *.tar.gz ]]; then
        tar -xzf "$file" -C "$dir"
    fi
done

echo "✅ All files extracted!"