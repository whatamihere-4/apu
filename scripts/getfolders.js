(() => {
    if (typeof appdata === 'undefined' || !appdata.fileManager?.mainContent?.data) {
      return null;
    }
    const data = appdata.fileManager.mainContent.data;
    const children = data.children || {};
    const folderMap = {};
    Object.keys(children).forEach((key) => {
      const item = children[key];
      if (item.type === 'folder') {
        folderMap[item.id] = item.name;
      }
    });
    return folderMap;
  })();