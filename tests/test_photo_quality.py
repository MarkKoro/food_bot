"""Offline source-image and model-request tests. No production providers."""
import asyncio, io, json, os, unittest
from unittest.mock import patch
from PIL import Image
os.environ.setdefault('BOT_TOKEN','123:TEST')
os.environ.setdefault('OPENAI_API_KEY','offline-test')
os.environ.setdefault('CHANNEL_ID','-1001')
import bot
from food_analysis import prepare_image, image_mime, read_response, response_format, FoodAnalysisError, FoodFormatError
from nutrients import NUTRIENTS


def picture(size=(200,100),fmt='JPEG',exif=None):
    out=io.BytesIO();image=Image.new('RGB',size,(200,100,40));args={'format':fmt}
    if exif:args['exif']=exif
    image.save(out,**args);return out.getvalue()


def recognized():
    return {'nutrient_schema_version':1,'status':'recognized','note':'Размер порции примерный.',
            'items':[{'name':'Рис','grams':150,'kcal':200,'protein_g':5,'fat_g':1,'carbs_g':43,
                      'nutrients_per_100g':{'iron':1.2,'vitamin_d':None}}],
            'totals':{'kcal':9999,'protein_g':0,'fat_g':0,'carbs_g':0}}


def answer(value,finish='stop',refusal=None):
    return {'choices':[{'finish_reason':finish,'message':{'content':json.dumps(value),'refusal':refusal}}]}


class PhotoQuality(unittest.TestCase):
    def test_small_jpeg_is_not_recompressed(self):
        data=picture();self.assertEqual(prepare_image(data),data)
    def test_orientation_is_corrected_even_below_old_byte_threshold(self):
        exif=Image.Exif();exif[274]=6;data=picture(exif=exif)
        result=prepare_image(data)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size,(100,200));self.assertNotIn(274,image.getexif())
    def test_large_low_byte_file_is_resized_without_upscaling(self):
        data=picture((4000,1000));self.assertLess(len(data),2_500_000)
        with Image.open(io.BytesIO(prepare_image(data))) as image:self.assertEqual(image.size,(2048,512))
        with Image.open(io.BytesIO(prepare_image(picture((100,100))))) as image:self.assertEqual(image.size,(100,100))
    def test_webp_signature_and_alpha_background(self):
        self.assertEqual(image_mime(picture(fmt='WEBP')),'image/webp')
        out=io.BytesIO();Image.new('RGBA',(32,32),(0,0,0,0)).save(out,format='PNG')
        with Image.open(io.BytesIO(prepare_image(out.getvalue()))) as image:self.assertGreater(image.getpixel((0,0))[0],245)
    def test_corrupt_oversize_animation_and_excess_pixels_are_rejected(self):
        for data in (b'not an image',b'x'*(20*1024*1024+1),picture((6000,5000))):
            with self.assertRaises(FoodAnalysisError):prepare_image(data)
        out=io.BytesIO();Image.new('RGB',(12,12),'red').save(out,format='GIF',save_all=True,append_images=[Image.new('RGB',(12,12),'blue')])
        with self.assertRaises(FoodAnalysisError):prepare_image(out.getvalue())
    def test_strict_schema_all_codes_and_arbitrary_model_override(self):
        for model in ('gpt-4o','gpt-4o-mini'):
            schema=response_format(model)['json_schema']['schema']
            nutrients=schema['properties']['items']['items']['properties']['nutrients_per_100g']
            self.assertEqual(set(nutrients['required']),set(NUTRIENTS))
            self.assertFalse(nutrients['additionalProperties'])
        self.assertEqual(response_format('owner-custom-model'),{'type':'json_object'})
    def test_portion_totals_are_arithmetic_and_unknown_nutrients_stay_unknown(self):
        value=read_response(answer(recognized()))
        self.assertEqual(value['totals']['kcal'],200)
        self.assertEqual(len(value['items'][0]['nutrients_per_100g']),54)
        self.assertIsNone(value['items'][0]['nutrients_per_100g']['vitamin_d'])
    def test_valid_zero_energy_water_is_not_an_empty_meal(self):
        value=recognized();value['items'][0].update(name='Вода',kcal=0,protein_g=0,fat_g=0,carbs_g=0)
        self.assertEqual(read_response(answer(value))['totals']['kcal'],0)
    def test_refusal_truncation_and_nonfood_are_user_correctable_not_format_retries(self):
        for result in (answer(recognized(),finish='length'),answer(recognized(),refusal='no'),answer({'status':'no_food','items':[]})):
            with self.assertRaises(FoodAnalysisError) as error:read_response(result)
            self.assertNotIsInstance(error.exception,FoodFormatError)
    def test_unreasonable_macro_output_is_rejected(self):
        for number in (-1,float('nan'),True):
            data=recognized();data['items'][0]['kcal']=number
            with self.assertRaises(FoodFormatError):read_response(answer(data))
    def call(self,responses,image=None):
        recorded=[]
        class Response:
            def __init__(self,data):self.data=data
            def raise_for_status(self):pass
            def json(self):return self.data
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):recorded.append(kwargs['json']);return Response(responses.pop(0))
        async def run():
            bot._OPENAI_SEM=asyncio.Semaphore(3)
            if image is not None:return await bot.analyze_food_photo(image,'Рис 150 г')
            return await bot.analyze_food_text('Рис 150 г')
        with patch.object(bot.httpx,'AsyncClient',lambda **kwargs:Client()):
            try:return asyncio.run(run()),recorded
            except Exception as e:return e,recorded
    def test_photo_and_text_each_use_one_call_and_one_nutrient_contract(self):
        for image in (None,picture()):
            value,calls=self.call([answer(recognized())],image)
            self.assertIsInstance(value,dict);self.assertEqual(len(calls),1)
            self.assertEqual(calls[0]['response_format']['type'],'json_schema')
            self.assertEqual(calls[0]['messages'][0]['content'].count('iron: mg'),1)
    def test_only_malformed_photo_can_use_one_repair_attempt(self):
        value,calls=self.call([answer({}),answer(recognized())],picture());self.assertIsInstance(value,dict);self.assertEqual(len(calls),2)
        for payload in [answer({'status':'no_food'}),answer(recognized(),finish='length')]:
            value,calls=self.call([payload],picture());self.assertIsInstance(value,FoodAnalysisError);self.assertEqual(len(calls),1)
        value,calls=self.call([],b'corrupt');self.assertIsInstance(value,FoodAnalysisError);self.assertEqual(calls,[])

    def test_owner_cache_identical_photo_text_no_new_provider_and_changed_input_recomputed(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock
        from progressor_ai_cache import purge
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'DB_PATH',str(Path(folder)/'food.db')):
            provider=AsyncMock(return_value=read_response(answer(recognized())))
            async def run():
                async def owned(uid,text):
                    marker=bot._food_ai_action.set((uid,asyncio.current_task()))
                    try:return await bot.analyze_food_text(text)
                    finally:bot._food_ai_action.reset(marker)
                with patch.object(bot,'_food_model_request',provider):
                    a=await owned(1,'Рис 150 г');self.assertNotIn('_progressor_cache_hit',a)
                    b=await owned(1,'Рис 150 г');self.assertTrue(b['_progressor_cache_hit']);self.assertEqual(provider.await_count,1)
                    await owned(2,'Рис 150 г');await owned(1,'Рис 200 г');self.assertEqual(provider.await_count,3)
                    purge(bot.DB_PATH,1);await owned(1,'Рис 150 г');self.assertEqual(provider.await_count,4)
                    marker=bot._food_ai_action.set((1,asyncio.current_task()))
                    try:
                        c=await bot.analyze_food_photo(picture());d=await bot.analyze_food_photo(picture())
                        self.assertNotIn('_progressor_cache_hit',c);self.assertTrue(d['_progressor_cache_hit']);self.assertEqual(provider.await_count,5)
                    finally:bot._food_ai_action.reset(marker)
            asyncio.run(run())
