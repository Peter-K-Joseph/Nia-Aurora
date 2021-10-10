import asyncio
import functools
import itertools
import math
import os
import random
import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands
from youtube_dl.utils import PostProcessingError
import pprint
import speedtest

# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(
            cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(
                'Sorry, But I couldn\'t find anything that matched `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(
                    'Sorry, But I couldn\'t find anything that matched `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(
            cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError(
                        'Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(
                                   self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .add_field(name="Views", value='({0.source.views})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))
        return embed

    def currentPlayerURL(self):
        return ['{0.source.title}'.format(self),'{0.source.url}'.format(self)]


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage(
                'This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError(
                'Okay.. I think you are connected to a voice channel.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""
        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""
        try:
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')
            await ctx.send('Got it! Paused...')
        except:
            await ctx.send('Hey, I don\'t think i\'m playing anything')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""
        try:
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')
            await ctx.send('Got it! Resuming...')
        except:
            await ctx.send('Hey, I don\'t think i\'m playing anything')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()
        await ctx.send('Music Stopped')

        try:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')
        except:
            await ctx.send("OOO... Naa.. No Can Do.. You aren't playing any")

    @commands.command(name='sendsong')
    @commands.has_permissions(manage_guild=True)
    async def _download(self, ctx: commands.Context):
        """Send the currently playing music."""
        print("[CONSOLE] ATTEMPTING TO DOWNLOAD A MUSIC")
        await ctx.send("Okay... Preparing to send " + ctx.voice_state.current.currentPlayerURL()[0])
        try:
            isFile = os.path.isfile("song.mp3")
            if isFile:
                os.remove("song.mp3")
                print("[INFO] USER REQUESTED DELETE COMMAND COMPLETE")
        except PermissionError:
            print("[ERROR] AN ERROR OCCURRED WHILE DELEING A FILE. FILE IS BEING USED")
        try:
            await ctx.send("Ill let you know once the download is complete")
            try:
                with youtube_dl.YoutubeDL({'format': 'bestaudio/best','outtmpl': 'song.mp3','postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '198',}]}) as ydl:
                    ydl.download([ctx.voice_state.current.currentPlayerURL()[1]])
                await ctx.send("Download Complete. Sending you the song file")
                await ctx.send(file=discord.File(r'./song.mp3'))
                print("[CONSOLE] A FILE DOWNLOAD WAS COMEPLETED")
            except:
                print("[ERROR] UNKNOWN ERROR OCCURRED WHILE DOWNLOADING CONTENT FROM '{}'".format(ctx.voice_state.current.currentPlayerURL()[1]))
        except:
            await ctx.send("Opps! I screwed up from my end! Sorry... I dont think I can send that song")

    @commands.command(name='shutdown')
    @commands.has_permissions(manage_guild=True)
    async def _shutdown(self, ctx: commands.Context):
        """Shutdown Nia [You will need Peter to start this once you do this]."""
        ctx.voice_state.songs.clear()
        await ctx.send("Shutting Down")
        exit()

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Ummm.. I don\'t think i\'m playing anythinf right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, **{} of 3** has decided to skip'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('There\' nothing in the queue list.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        await ctx.send("Okay... This is the current queue list")
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(
                i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Okay... STACK UNDERFLOW!!!')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')
        return await ctx.send('Okay... Rolling the dice... aaaaaaannndddd')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('And this is something that Gnana Prakshi maam would call as Stack Overflow')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')
        return await ctx.send('Okay... Dequeue operation done.. [TOP = TOP + 1]😗')

    @commands.command(name='thanks')
    async def _loop(self, ctx: commands.Context):
        """Just a token of appreciation"""
        return await ctx.send('Always my pleasure... 😗')
        
    @commands.command(name='clearmsg', pass_context = True)
    async def deleteText(self, ctx: commands.Context, *, number: str):
        """Delete n number of message using nia clear-msg <int>"""
        number = int(number) #Converting the amount of messages to delete to an integer
        async for x in ctx.history(limit = number):
            # print(x.id)
            try:
                await x.delete()
            except:
                print("[CONSOLE] MISSING PERMISSION")
        await ctx.send('Deleted ' + str(number) + " of messages from current channel")
        
        
    @commands.command(name='load')
    async def loadCall(self, ctx: commands.Context):
        """Show System Stats related to server performance"""
        await ctx.send("Getting Stats. Starting Performance Monitor...")
        embed = discord.Embed(
            title="Nia MusicBot",
            description="All Systems Operational",
            color=discord.Color.blue())
        embed.add_field(name='Download', value=str(round(speedtest.Speedtest().download()/102400, 2)) + ' MB')
        embed.add_field(name='Upload', value=str(round(speedtest.Speedtest().upload()/102400, 2)) + ' MB')
        embed.set_footer(text="Last Error: SOCKET CONNECTION FAILURE TO A LIVE SERVER (ERROR 403)")

        return await ctx.send(embed=embed)
    
    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loop a song that you are playing"""
        if not ctx.voice_state.is_playing:
            return await ctx.send('Oooo... I don\'t think I\'m currently playing anything...')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.send('Ayy Ayy. Captain!')
        await ctx.message.add_reaction('✅')

    @commands.command(name='state')
    async def _state(self, ctx: commands.Context):
        """Show System Stats related to application performance"""
        embed = discord.Embed(
            title="Nia MusicBot",
            description="All Systems Operational",
            color=discord.Color.blue())
        embed.add_field(name='Codename', value='mus.io')
        embed.add_field(name='Version', value='v1.05')
        embed.add_field(name='Server', value='Raspberry Pi 4B')
        embed.add_field(
            name='State', value='Multiple services down')
        embed.add_field(name='Audio Playback', value='198kbps')
        embed.add_field(name='Audio Profile', value='Best Available Audio')
        embed.set_footer(
            text="Owned and managed by Peter K Joseph. Thanks for using me :)")

        return await ctx.send(embed=embed)

# Plays the Song
    @ commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Play any song or from link. Default search engine is youtube and I have 0 plans of adding anymore"""
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
                await bot.change_presence(activity=discord.Streaming(name=str(source.title), url=str(source.url)))
            except YTDLError as e:
                await ctx.send('Opps.. An internal error occurred while doing that: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Added {} into the queue'.format(str(source)))

    @ _join.before_invoke
    @ _play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError(
                'Hey, Join a voice channel so that I can join you...')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError(
                    'Hey, I\'m already in a voice channel...')


bot = commands.Bot(command_prefix='nia ',
                   description='Yet another musicbot waiting to get a seize and desist letter from Google or Youtube')
bot.add_cog(Music(bot))


@ bot.event
async def on_ready():
    print('Logged in as:\n{0.user.name}\n{0.user.id}'.format(bot))

bot.run("ODc4OTU3OTY0MTQzMjM1MTQz.YSIvZA.Q01FQQjV9OjD_A27VQgFa0aySqs")
