" awnix desktop -- neovim defaults (/etc/xdg/nvim/sysinit.vim). No plugins, nothing
" fetched. Your ~/.config/nvim/init.lua (or init.vim) loads after this and wins.
if filereadable('/usr/share/nvim/sysinit.vim')
  source /usr/share/nvim/sysinit.vim
endif
let mapleader = ' '
set number relativenumber signcolumn=yes cursorline
set mouse=a clipboard=unnamedplus
set expandtab shiftwidth=4 tabstop=4 smartindent
set ignorecase smartcase incsearch hlsearch
set splitright splitbelow scrolloff=8
set undofile updatetime=300 termguicolors
silent! colorscheme habamax
nnoremap <leader>w <cmd>write<cr>
nnoremap <leader>q <cmd>quit<cr>
nnoremap <leader>e <cmd>Explore<cr>
nnoremap <leader>f :find **/
nnoremap <leader>h <cmd>nohlsearch<cr>
nnoremap <C-h> <C-w>h
nnoremap <C-j> <C-w>j
nnoremap <C-k> <C-w>k
nnoremap <C-l> <C-w>l
